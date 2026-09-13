"""NVIDIA 驱动「电源管理模式」的读写：把 NVIDIA 控制面板里那一项做成程序开关。

对应 ninfer 启动器「控制」页「通用设置」组的「性能模式」开关。改的就是 NVIDIA 控制面板 →
管理 3D 设置 → 全局设置 → **电源管理模式** 这一项，走驱动自己的 DRS（Driver
Settings）接口写全局 profile，效果与手点控制面板等价、重启后保持。

判据来自 NVAPI 官方头文件 ``NvApiDriverSettings.h``（本机 2026-08-30 实测核对过）::

    PREFERRED_PSTATE_ID     = 0x1057EB71   settingName = "Power management mode"
    PREFERRED_PSTATE_ADAPTIVE                    = 0
    PREFERRED_PSTATE_PREFER_MAX                  = 1   ← 「最高性能优先」
    PREFERRED_PSTATE_DRIVER_CONTROLLED           = 2
    PREFERRED_PSTATE_PREFER_CONSISTENT_PERFORMANCE = 3
    PREFERRED_PSTATE_PREFER_MIN                  = 4
    PREFERRED_PSTATE_OPTIMAL_POWER               = 5   ← 出厂默认，界面显示「正常」

本模块只做「读一个值、写一个值」。开关放在界面哪一格、状态怎么显示是
``ui/`` 的事；**这个值刻意不进 ``params/registry.py``**——那是 ninfer-serve 的全部
命令行参数的唯一事实源（registry 契约 1），混一个非 CLI 项进去会顺着
``ParamsTabs.get_values()`` 漏进预设文件和命令行组装。

隐性契约（改动前务必读完）——本模块几乎每一条都属于「做错了验证不会变红」：

1. **结构体版本号必须用 ``sizeof(结构体) | (版本 << 16)`` 现场算，不许写死**
   （:func:`_make_version`）。本机实测这个值是 ``0x00013020``（sizeof=12320），
   但一旦有人调整了 :class:`NVDRS_SETTING` 的字段，写死的常量就与真实布局不符，
   而 NVAPI 只会按它自己以为的布局去读那块内存——轻则返回
   ``NVAPI_INCOMPATIBLE_STRUCT_VERSION``（-8，这算幸运），重则读到越界数据。
2. **字段定义不能「顺手简化」**。``settingName`` 是 ``NvU16[2048]``（4096 字节）、
   两个 union 各 4100 字节（``NVDRS_BINARY_SETTING`` = u32 长度 + 4096 字节数据）。
   把它们改成看起来等价的 ``c_wchar * 2048`` 或砍掉「反正用不到」的 union，
   sizeof 立刻变，契约 1 那个版本号跟着变，NVAPI 直接不认。用不到的字段也必须占位。
3. **``NvAPI_DRS_SetSetting`` 之后必须 ``NvAPI_DRS_SaveSettings``**。少了这一步，
   改动只落在内存里的 session 上，session 一销毁就没了——而且 SetSetting 返回 0，
   界面会显示「已开启」，重启后发现什么都没变，且没有任何一处报错。
4. **session 与 NVAPI 都必须成对释放**（:class:`_DrsSession` 用上下文管理器保证）。
   漏掉 ``DestroySession`` / ``Unload`` 不会立刻出问题，只是每次开关都泄漏一个驱动侧
   句柄；本程序寿命长、开关可反复点，攒够了会开始失败。
5. **写的是 base profile（全局设置），不是某个应用 profile**。写进应用 profile 的话
   得先按 exe 名建 profile，而 ninfer-serve 是被本程序当子进程拉起的，profile 匹配
   与否取决于驱动怎么认那个进程——赌不起。全局设置的语义明确：对所有程序生效。
6. **不在导入期加载 ``nvapi64.dll``**。没有 NVIDIA 卡（或只有集显）的机器上
   ``WinDLL`` 会直接抛异常，放在模块顶层等于整个启动器 import 就崩。
   :func:`_load_nvapi` 在每次调用时才加载，失败时抛 :class:`NvapiUnavailableError`。
7. **顶层三个函数（:func:`read_power_mode` / :func:`apply_power_mode` /
   :func:`describe_availability`）一律不抛异常**，返回带 ``ok`` 与中文 ``message``
   的 :class:`PowerModeResult`。这是给界面开关直接用的：一个开关点下去要么生效、
   要么回弹并说清原因，不能让一个 ctypes 异常穿到 Qt 事件循环里变成静默失效。
8. **「关闭性能模式」= 写回驱动出厂默认 :attr:`PowerMode.OPTIMAL_POWER`，不是
   「恢复你原先手动设过的档位」**。本模块刻意不持久化「开启前是什么值」：那份状态一旦
   与驱动实际值不同步（用户中途自己去控制面板改了），恢复动作就会把用户的设置改成一个
   陈旧值，而这个错误没有任何迹象。语义如实标注在 :data:`DISABLE_MODE` 上，
   界面 tooltip 须原样告知使用者。
9. **界面开关的状态以驱动当前值为事实源**（每次用 :func:`read_power_mode` 读），
   不另存一份到 ``settings.json``。存了就有两份事实，而用户完全可以绕过本程序去
   控制面板改——两份必然漂移，且漂移后界面显示的开关状态是错的。
"""

from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass
from enum import IntEnum

__all__ = [
    "NVAPI_DLL_NAME",
    "PREFERRED_PSTATE_ID",
    "PREFERRED_PSTATE_SETTING_NAME",
    "DISABLE_MODE",
    "ENABLE_MODE",
    "GpuPowerError",
    "NvapiUnavailableError",
    "NvapiCallError",
    "PowerMode",
    "PowerModeResult",
    "describe_availability",
    "read_power_mode",
    "apply_power_mode",
]

#: 64 位 NVAPI 运行库。随显卡驱动安装到 ``System32``，不随本程序分发。
NVAPI_DLL_NAME = "nvapi64.dll"

#: 「电源管理模式」的 DRS 设置 ID（``NvApiDriverSettings.h`` 的 ``PREFERRED_PSTATE_ID``）。
PREFERRED_PSTATE_ID = 0x1057EB71

#: 该设置在驱动里的英文名。读出来与它比对是一道廉价的自检：对得上就说明
#: 设置 ID、结构体布局、版本号三者全对，而不只是「调用返回了 0」。
PREFERRED_PSTATE_SETTING_NAME = "Power management mode"

#: ``NVDRS_SETTING_TYPE`` 的 ``NVDRS_DWORD_TYPE``。
_NVDRS_DWORD_TYPE = 0

#: ``NvAPI_UnicodeString`` 的元素数（``NVAPI_UNICODE_STRING_MAX``）。
_NVAPI_UNICODE_STRING_MAX = 2048

#: ``NVDRS_BINARY_SETTING.valueData`` 的字节数（``NVAPI_BINARY_DATA_MAX``）。
_NVAPI_BINARY_DATA_MAX = 4096

#: ``NvAPI_ShortString`` 的字节数，:func:`_error_text` 用它接错误串。
_NVAPI_SHORT_STRING_MAX = 64

#: NVAPI 成功返回码。
_NVAPI_OK = 0

#: ``nvapi_QueryInterface`` 的函数 ID。NVAPI 不导出具名符号，只能按 ID 取函数指针；
#: 这批 ID 在本机 2026-08-30 实测全部可解析（见模块头部的核对说明）。
#: 取不到时 QueryInterface 返回 NULL，:meth:`_Nvapi._resolve` 会明确报出是哪一个——
#: 不允许静默跳过，缺一个函数后面整条链路都会以看不懂的方式失败。
_FUNCTION_IDS = {
    "NvAPI_Initialize": 0x0150E828,
    "NvAPI_Unload": 0xD22BDD7E,
    "NvAPI_GetErrorMessage": 0x6C2D048C,
    "NvAPI_DRS_CreateSession": 0x0694D52E,
    "NvAPI_DRS_DestroySession": 0xDAD9CFF8,
    "NvAPI_DRS_LoadSettings": 0x375DBD6B,
    "NvAPI_DRS_SaveSettings": 0xFCBC7E14,
    "NvAPI_DRS_GetBaseProfile": 0xDA8466A0,
    "NvAPI_DRS_SetSetting": 0x577DD202,
    "NvAPI_DRS_GetSetting": 0x73BF8338,
}


class PowerMode(IntEnum):
    """``PREFERRED_PSTATE`` 的取值（数值取自 NVAPI 官方头文件）。"""

    ADAPTIVE = 0
    PREFER_MAX = 1
    DRIVER_CONTROLLED = 2
    PREFER_CONSISTENT_PERFORMANCE = 3
    PREFER_MIN = 4
    OPTIMAL_POWER = 5

    @property
    def label(self) -> str:
        """中文名，尽量与 NVIDIA 控制面板的中文界面一致。"""
        return _MODE_LABELS.get(self, f"未知档位（{int(self)}）")


#: 档位 → 中文名。``ADAPTIVE`` 与 ``OPTIMAL_POWER`` 在新版控制面板里都显示成
#: 「正常」一类的措辞，这里各自加括号注明原始档位名，免得界面上出现两个「正常」。
_MODE_LABELS = {
    PowerMode.ADAPTIVE: "自适应",
    PowerMode.PREFER_MAX: "最高性能优先",
    PowerMode.DRIVER_CONTROLLED: "由驱动控制",
    PowerMode.PREFER_CONSISTENT_PERFORMANCE: "一致性能优先",
    PowerMode.PREFER_MIN: "最低功耗优先",
    PowerMode.OPTIMAL_POWER: "正常（最佳功耗）",
}

#: 「开启性能模式」写入的档位。
ENABLE_MODE = PowerMode.PREFER_MAX

#: 「关闭性能模式」写入的档位——驱动出厂默认，**不是**「恢复用户原先的档位」
#: （契约 8）。界面 tooltip 必须如实说明这一点。
DISABLE_MODE = PowerMode.OPTIMAL_POWER


class GpuPowerError(RuntimeError):
    """本模块所有失败的基类，供调用方一次性捕获。"""


class NvapiUnavailableError(GpuPowerError):
    """NVAPI 本身不可用：非 Windows、驱动未安装、dll 缺失、函数 ID 解析不出。"""


class NvapiCallError(GpuPowerError):
    """NVAPI 调用返回了非 0 状态码。

    :param what: 中文描述的动作名，用于拼消息
    :param status: NVAPI 返回码
    :param detail: ``NvAPI_GetErrorMessage`` 给出的英文错误串；取不到时为空
    """

    def __init__(self, what: str, status: int, detail: str = "") -> None:
        self.what = what
        self.status = status
        self.detail = detail
        text = f"{what}失败（NVAPI 状态码 {status}"
        if detail:
            text += f"：{detail}"
        text += "）"
        super().__init__(text)


# ---------------------------------------------------------------------------
# ctypes 结构体（契约 1、2：字段一个都不能省，版本号现场算）
# ---------------------------------------------------------------------------

_NvAPI_UnicodeString = ctypes.c_uint16 * _NVAPI_UNICODE_STRING_MAX


class NVDRS_BINARY_SETTING(ctypes.Structure):
    """``NVDRS_BINARY_SETTING``。本模块只写 DWORD 型设置，但它决定 union 的 sizeof。"""

    _fields_ = [
        ("valueLength", ctypes.c_uint32),
        ("valueData", ctypes.c_uint8 * _NVAPI_BINARY_DATA_MAX),
    ]


class _SettingValueUnion(ctypes.Union):
    """``NVDRS_SETTING`` 里两个匿名 union 的形状（DWORD / 宽字符串 / 二进制）。"""

    _fields_ = [
        ("u32Value", ctypes.c_uint32),
        ("wszValue", _NvAPI_UnicodeString),
        ("binaryValue", NVDRS_BINARY_SETTING),
    ]


class NVDRS_SETTING(ctypes.Structure):
    """``NVDRS_SETTING_V1``。字段顺序与类型必须与 NVAPI 头文件逐字对应（契约 2）。"""

    _fields_ = [
        ("version", ctypes.c_uint32),
        ("settingName", _NvAPI_UnicodeString),
        ("settingId", ctypes.c_uint32),
        ("settingType", ctypes.c_uint32),
        ("settingLocation", ctypes.c_uint32),
        ("isCurrentPredefined", ctypes.c_uint32),
        ("isPredefinedValid", ctypes.c_uint32),
        ("predefined", _SettingValueUnion),
        ("current", _SettingValueUnion),
    ]


def _make_version(struct_type: type[ctypes.Structure], version: int) -> int:
    """NVAPI 的 ``MAKE_NVAPI_VERSION``：``sizeof(结构体) | (版本 << 16)``（契约 1）。"""
    return ctypes.sizeof(struct_type) | (version << 16)


def _read_unicode_string(buffer: "ctypes.Array[ctypes.c_uint16]") -> str:
    """把 ``NvAPI_UnicodeString`` 读成 Python 字符串，截到第一个 NUL。"""
    chars: list[str] = []
    for code in buffer:
        if not code:
            break
        chars.append(chr(code))
    return "".join(chars)


# ---------------------------------------------------------------------------
# NVAPI 句柄与 DRS session
# ---------------------------------------------------------------------------


class _Nvapi:
    """已加载并解析好函数指针的 NVAPI 实例（契约 6：只在被调用时构造）。"""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise NvapiUnavailableError(
                f"当前系统是 {sys.platform}，NVIDIA 电源管理模式只在 Windows 上可设置"
            )
        try:
            self._dll = ctypes.WinDLL(NVAPI_DLL_NAME)
        except OSError as exc:
            raise NvapiUnavailableError(
                f"找不到或无法加载 {NVAPI_DLL_NAME}（{exc}）。"
                "这台机器可能没有 NVIDIA 显卡，或显卡驱动未正确安装"
            ) from exc

        try:
            query = self._dll.nvapi_QueryInterface
        except AttributeError as exc:
            raise NvapiUnavailableError(
                f"{NVAPI_DLL_NAME} 里没有 nvapi_QueryInterface，这个 dll 不是预期的 NVAPI 运行库"
            ) from exc
        query.restype = ctypes.c_void_p
        query.argtypes = [ctypes.c_uint32]
        self._query = query

        self._pointers = {name: self._resolve(name, fid) for name, fid in _FUNCTION_IDS.items()}

        # 签名一律显式声明：NVAPI 全是 __cdecl（含 64 位 Windows），
        # 用 CFUNCTYPE 而不是 WINFUNCTYPE。
        self._initialize = self._bind("NvAPI_Initialize", ctypes.c_int32)
        self._unload = self._bind("NvAPI_Unload", ctypes.c_int32)
        self._get_error_message = self._bind(
            "NvAPI_GetErrorMessage", ctypes.c_int32, ctypes.c_int32, ctypes.c_char_p
        )
        self._create_session = self._bind(
            "NvAPI_DRS_CreateSession", ctypes.c_int32, ctypes.c_void_p
        )
        self._destroy_session = self._bind(
            "NvAPI_DRS_DestroySession", ctypes.c_int32, ctypes.c_void_p
        )
        self._load_settings = self._bind(
            "NvAPI_DRS_LoadSettings", ctypes.c_int32, ctypes.c_void_p
        )
        self._save_settings = self._bind(
            "NvAPI_DRS_SaveSettings", ctypes.c_int32, ctypes.c_void_p
        )
        self._get_base_profile = self._bind(
            "NvAPI_DRS_GetBaseProfile", ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p
        )
        self._set_setting = self._bind(
            "NvAPI_DRS_SetSetting",
            ctypes.c_int32,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )
        self._get_setting = self._bind(
            "NvAPI_DRS_GetSetting",
            ctypes.c_int32,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
        )

    def _resolve(self, name: str, function_id: int) -> int:
        pointer = self._query(function_id)
        if not pointer:
            raise NvapiUnavailableError(
                f"NVAPI 里解析不出 {name}（ID 0x{function_id:08X}）。"
                "驱动版本可能过旧，或这个函数已被移除"
            )
        return int(pointer)

    def _bind(self, name: str, restype: type, *argtypes: type):
        prototype = ctypes.CFUNCTYPE(restype, *argtypes)
        return prototype(self._pointers[name])

    def error_text(self, status: int) -> str:
        """取 NVAPI 的英文错误串；取不到时返回空串（不让「查错误原因」自己再失败）。"""
        buffer = ctypes.create_string_buffer(_NVAPI_SHORT_STRING_MAX)
        try:
            if self._get_error_message(ctypes.c_int32(status), buffer) != _NVAPI_OK:
                return ""
        except OSError:
            return ""
        return buffer.value.decode("ascii", errors="replace").strip()

    def check(self, status: int, what: str) -> None:
        """非 0 状态码一律抛 :class:`NvapiCallError`，附带英文错误串。"""
        if status != _NVAPI_OK:
            raise NvapiCallError(what, status, self.error_text(status))

    # -- 生命周期（契约 4） ------------------------------------------------

    def __enter__(self) -> "_Nvapi":
        self.check(self._initialize(), "NVAPI 初始化")
        return self

    def __exit__(self, *_exc_info: object) -> None:
        # 释放失败不再抛：调用方此刻要么已经成功、要么已经有一个更有意义的异常在飞，
        # 用一个「清理没做干净」盖掉真正的原因只会更难查。
        try:
            self._unload()
        except OSError:
            pass

    # -- DRS 操作 ----------------------------------------------------------

    def session(self) -> "_DrsSession":
        """建一个已 ``LoadSettings`` 的 DRS session（用作上下文管理器）。"""
        return _DrsSession(self)


class _DrsSession:
    """DRS session + base profile 句柄，退出时保证销毁（契约 4）。"""

    def __init__(self, api: _Nvapi) -> None:
        self._api = api
        self._handle = ctypes.c_void_p()
        self._profile = ctypes.c_void_p()

    def __enter__(self) -> "_DrsSession":
        api = self._api
        api.check(api._create_session(ctypes.byref(self._handle)), "创建驱动设置会话")
        try:
            api.check(api._load_settings(self._handle), "载入驱动设置")
            # 契约 5：全局设置写 base profile，不按 exe 名建应用 profile。
            api.check(
                api._get_base_profile(self._handle, ctypes.byref(self._profile)),
                "取驱动全局设置档案",
            )
        except GpuPowerError:
            self._destroy()
            raise
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self._destroy()

    def _destroy(self) -> None:
        if self._handle.value:
            try:
                self._api._destroy_session(self._handle)
            except OSError:
                pass
            self._handle = ctypes.c_void_p()

    def get_dword(self, setting_id: int, what: str) -> tuple[int, str]:
        """读一个 DWORD 型设置，返回 ``(当前值, 驱动记录的英文设置名)``。"""
        setting = NVDRS_SETTING()
        setting.version = _make_version(NVDRS_SETTING, 1)
        self._api.check(
            self._api._get_setting(
                self._handle, self._profile, ctypes.c_uint32(setting_id), ctypes.byref(setting)
            ),
            what,
        )
        return int(setting.current.u32Value), _read_unicode_string(setting.settingName)

    def set_dword(self, setting_id: int, value: int, what: str) -> None:
        """写一个 DWORD 型设置并落盘（契约 3：SetSetting 之后必须 SaveSettings）。"""
        setting = NVDRS_SETTING()
        setting.version = _make_version(NVDRS_SETTING, 1)
        setting.settingId = setting_id
        setting.settingType = _NVDRS_DWORD_TYPE
        setting.current.u32Value = value
        self._api.check(
            self._api._set_setting(self._handle, self._profile, ctypes.byref(setting)), what
        )
        self._api.check(self._api._save_settings(self._handle), f"{what}后保存驱动设置")


# ---------------------------------------------------------------------------
# 结果类型与顶层接口（契约 7：不抛异常）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PowerModeResult:
    """一次读或写的结果。

    :param ok: 是否成功
    :param mode: 成功时为**操作后驱动的实际档位**（写操作会回读一次核对，见
        :func:`apply_power_mode`）；失败时为 ``None``
    :param message: 中文说明，成功与失败都有内容，可直接写日志区
    :param setting_name: 驱动记录的英文设置名；只在读取成功时有值，用于自检
    """

    ok: bool
    mode: PowerMode | None
    message: str
    setting_name: str = ""

    @property
    def performance_enabled(self) -> bool:
        """当前是否处于「性能模式」（:data:`ENABLE_MODE`）。

        失败时恒为假——**「读不到」不等于「已关闭」**，但界面开关必须有个位置放，
        放在「关」这一侧配合 :attr:`ok` 为假时禁用开关，比放在「开」一侧更不容易误导。
        调用方应先看 :attr:`ok`。
        """
        return self.ok and self.mode is ENABLE_MODE

    def format_lines(self) -> tuple[str, ...]:
        """给日志区的单行结果，与 ``core/health.py`` 的同名方法保持同一种用法。"""
        return (self.message,)


def _failure(exc: GpuPowerError) -> PowerModeResult:
    return PowerModeResult(ok=False, mode=None, message=str(exc))


def _to_mode(value: int) -> PowerMode | None:
    try:
        return PowerMode(value)
    except ValueError:
        return None


def describe_availability() -> PowerModeResult:
    """只探测「NVAPI 能不能用」，不读也不写设置。

    界面构造期用它决定开关是否可点：这一步不碰 DRS session，代价比
    :func:`read_power_mode` 小，且失败原因同样具体。

    :return: :attr:`PowerModeResult.ok` 为真表示 NVAPI 可用；:attr:`PowerModeResult.mode`
        恒为 ``None``（本函数不读档位）
    """
    try:
        with _Nvapi():
            return PowerModeResult(ok=True, mode=None, message="NVIDIA 驱动接口可用")
    except GpuPowerError as exc:
        return _failure(exc)


def read_power_mode() -> PowerModeResult:
    """读当前的电源管理模式（契约 9：界面开关的状态以这里为事实源）。

    :return: 成功时 :attr:`PowerModeResult.mode` 为当前档位；档位是官方枚举之外的
        未知值时 ``mode`` 为 ``None`` 但 ``ok`` 仍为真，消息里带上原始数字——
        驱动新增档位时不该让开关整个不可用
    """
    try:
        with _Nvapi() as api, api.session() as session:
            value, setting_name = session.get_dword(
                PREFERRED_PSTATE_ID, "读取电源管理模式"
            )
    except GpuPowerError as exc:
        return _failure(exc)

    mode = _to_mode(value)
    if mode is None:
        return PowerModeResult(
            ok=True,
            mode=None,
            message=f"当前电源管理模式是驱动的未知档位（原始值 {value}）",
            setting_name=setting_name,
        )
    return PowerModeResult(
        ok=True,
        mode=mode,
        message=f"当前电源管理模式：{mode.label}",
        setting_name=setting_name,
    )


def apply_power_mode(enable: bool) -> PowerModeResult:
    """开启或关闭性能模式。

    开启写 :data:`ENABLE_MODE`（最高性能优先），关闭写 :data:`DISABLE_MODE`
    （驱动出厂默认，**不是**恢复使用者原先手动设过的档位，见契约 8）。

    写完会**回读一次核对**：NVAPI 返回 0 只说明调用被接受，不等于值真的落进了 profile
    （契约 3 那种漏掉 SaveSettings 的情形就是「返回 0 但没生效」）。回读到的值与目标
    不符时返回 ``ok=False``，不报「已开启」。

    :param enable: 真为开启性能模式，假为关闭
    :return: 操作结果；``mode`` 为回读到的实际档位
    """
    target = ENABLE_MODE if enable else DISABLE_MODE
    action = "开启性能模式" if enable else "关闭性能模式"
    try:
        with _Nvapi() as api, api.session() as session:
            session.set_dword(PREFERRED_PSTATE_ID, int(target), action)
            value, setting_name = session.get_dword(
                PREFERRED_PSTATE_ID, f"{action}后回读电源管理模式"
            )
    except GpuPowerError as exc:
        return _failure(exc)

    actual = _to_mode(value)
    if value != int(target):
        return PowerModeResult(
            ok=False,
            mode=actual,
            message=(
                f"{action}未生效：驱动接受了写入但回读到的档位是"
                f"{actual.label if actual else f'未知值 {value}'}，"
                f"期望 {target.label}"
            ),
            setting_name=setting_name,
        )
    suffix = "" if enable else "（已恢复驱动出厂默认，不是你原先手动设过的档位）"
    return PowerModeResult(
        ok=True,
        mode=target,
        message=f"{action}成功，电源管理模式已设为{target.label}{suffix}",
        setting_name=setting_name,
    )
