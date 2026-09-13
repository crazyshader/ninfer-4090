"""预设与应用设置的读写。

配置根目录固定为用户级持久目录 %LOCALAPPDATA%/ninfer-launcher，与程序安装/构建位置解耦（重新打包、重装、多机分发都不丢用户数据）。
主题取值常量（THEME_DARK / THEME_LIGHT / THEME_SYSTEM / THEME_CHOICES）在本模块定义，
是主题设置字符串的唯一事实源。
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "ConfigStore",
    "resolve_config_root",
    "builtin_presets_dir",
    "seed_builtin_presets",
    "builtin_preset_names",
    "load_deleted_builtin",
    "record_builtin_deletion",
    "load_settings",
    "save_settings",
    "load_preset",
    "find_project_root",
    "save_preset",
    "delete_preset",
    "check_preset_name",
    "list_presets",
    "THEME_DARK",
    "THEME_LIGHT",
    "THEME_SYSTEM",
    "THEME_CHOICES",
]

#: 主题取值常量（需求：深色 / 浅色 / 跟随系统）。
#: 这些常量是主题设置字符串的唯一事实源：ui/theme.py、界面主题下拉框与
#: Settings.theme 字段都只认这几个名字，谁都不另写 "dark" / "light" / "system"
#: 字面量，避免拼写漂移成互相认不出的字符串而静默回落成默认深色。
THEME_DARK = "dark"
THEME_LIGHT = "light"
THEME_SYSTEM = "system"

#: 全部合法的主题设置取值，供下拉框枚举与合法性校验使用。
THEME_CHOICES: tuple[str, ...] = (THEME_DARK, THEME_LIGHT, THEME_SYSTEM)

_CONFIG_FILE = "settings.json"
_PRESETS_DIR = "presets"
# 记录「已被用户删除的内置预设」的墓碑文件（无 .json 扩展名，list_presets 只扫 *.json，不会被误当预设列出）。
_DELETED_BUILTIN_FILE = ".deleted_builtin_presets"


def resolve_config_root(probe: Any = None) -> Path:
    """判定配置根目录：用户级持久目录 %LOCALAPPDATA%/ninfer-launcher。

    预设与应用设置一律落在用户级目录，与程序安装/构建位置完全解耦：
    重新打包（build.ps1 -Clean）、重装、多机分发都不会丢失用户数据，
    且开发模式与打包版行为一致。LOCALAPPDATA 缺失时回落到用户主目录。
    """
    if probe is not None:
        return probe()
    local = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    root = local / "ninfer-launcher"
    root.mkdir(parents=True, exist_ok=True)
    return root


def project_root() -> Path:
    """程序根目录：打包后为 exe 所在目录（onedir 的应用目录），开发时为 launcher/ 目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent.parent


def find_project_root() -> Path:
    """定位项目根目录（包含 build-ninja/ 的那一层）。

    由 GUI 的 exe 自动探测（ui/control_panel._auto_detect、ui/main_window._resolve_exe）
    与 CLI 的 exe 自动探测（cli/resolve.py）共用。策略：从程序根目录
    （:func:`project_root`，已正确处理冻结/开发两种模式）逐级向上找含 build-ninja/
    的目录，最多 6 级。

    注意起点必须用 project_root()，不要写死 __file__ 的相对层级——本函数从 ui/ 搬到
    core/ 后，同样的「向上 3 级」会偏一级，而「向上最多 6 级」的容错会让它看起来
    仍然正常（见 docs/01-ninfer-launcher-cli.md 陷阱清单）。

    回归（docs 12.1）：首版迁移后循环体曾丢失 p = p.parent，6 次检查全落在同一个
    目录、总落回退分支，开发模式下回退值恰好正确，454 个测试无一变红。
    tests/test_config.py::TestFindProjectRoot 注入「比项目根深两级」的起点断言能向上
    找到含 build-ninja/ 的层，是防本 bug 复发的关卡。
    """
    base = project_root()
    p = base
    for _ in range(6):
        if p.is_dir() and (p / "build-ninja").is_dir():
            return p
        p = p.parent
    # 回退：直接取程序根的上级目录（与原实现一致）
    return base.parent


def builtin_presets_dir() -> Path | None:
    """随包分发的内置预设目录（resources/presets）。

    打包后 datas 的实际落位随 PyInstaller 版本而异（6.x onedir 放在 _internal/ 下），
    因此按候选顺序探测，取第一个存在的。找不到时返回 None（内置预设是可选资产，
    缺失不该让启动器起不来）。
    """
    root = project_root()
    candidates = [root / "resources" / _PRESETS_DIR, root / "_internal" / "resources" / _PRESETS_DIR]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def seed_builtin_presets(root: Path, builtin_dir: Path | None = None) -> list[str]:
    """把内置预设播种到配置根的 presets/ 目录。

    规则：只补「配置根里没有」的文件，绝不覆盖已有文件——用户改过的同名
    预设（哪怕是在预设列表里编辑后另存的）不能出厂重置；并且**跳过已被用户
    删除的内置预设**（见 :func:`record_builtin_deletion`），删掉的就别再自动
    冒回来。返回本次新播种的名字。
    """
    if builtin_dir is None:
        builtin_dir = builtin_presets_dir()
    if builtin_dir is None or not builtin_dir.is_dir():
        return []
    deleted = load_deleted_builtin(root)
    target_dir = root / _PRESETS_DIR
    seeded: list[str] = []
    for src in sorted(builtin_dir.glob("*.json")):
        if src.stem in deleted:
            continue
        dst = target_dir / src.name
        if dst.exists():
            continue
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            seeded.append(src.stem)
        except OSError:
            continue
    return seeded


def builtin_preset_names() -> frozenset[str]:
    """随包分发的内置（出厂）预设名集合。

    这些预设正常情况下由 :func:`seed_builtin_presets` 在启动时补回；但用户也可以
    删除它们——删除时把名字记入 :func:`record_builtin_deletion` 的墓碑名单，此后
    seed 就再不会把它们复活，删除是持久生效的。找不到内置目录时返回空集（内置
    预设是可选资产，缺失不该让启动器起不来）。
    """
    d = builtin_presets_dir()
    if d is None or not d.is_dir():
        return frozenset()
    return frozenset(f.stem for f in d.glob("*.json"))


def load_deleted_builtin(root: Path) -> frozenset[str]:
    """读取「已被用户删除的内置预设」名单。

    用户删掉一个内置（出厂）预设后，若不记录，下次启动 :func:`seed_builtin_presets`
    会从 ``resources/presets`` 把它补回来——表现成「删了又复活」。这份名单让删除
    持久生效：名单里列出的内置预设不再被播种。文件缺失/损坏时返回空集。
    """
    path = root / _DELETED_BUILTIN_FILE
    if not path.is_file():
        return frozenset()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return frozenset()
    if isinstance(data, list):
        return frozenset(x for x in data if isinstance(x, str))
    return frozenset()


def record_builtin_deletion(root: Path, name: str) -> None:
    """把 ``name`` 记入「已删除的内置预设」名单，使其不再被 seed 复活。"""
    root.mkdir(parents=True, exist_ok=True)
    deleted = set(load_deleted_builtin(root))
    deleted.add(name)
    path = root / _DELETED_BUILTIN_FILE
    path.write_text(json.dumps(sorted(deleted), ensure_ascii=False, indent=2), encoding="utf-8")


@dataclass
class Settings:
    """应用偏好设置。

    theme 字段取值见模块级 THEME_CHOICES；默认深色。主题与参数预设分开存储——
    主题走 settings.json，参数走 presets/ 目录下的文件，互不干扰。
    """
    last_preset: str = ""
    window_width: int = 1100
    window_height: int = 750
    model_dir: str = "E:\\ai\\ninfer-4090"
    exe_path: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    theme: str = THEME_DARK


def _norm_theme(value: Any) -> str:
    """把读出来的 theme 值归一成合法取值；非法或未知一律回落深色。"""
    if isinstance(value, str):
        text = value.strip().lower()
        if text in THEME_CHOICES:
            return text
    return THEME_DARK


def load_settings(root: Path) -> Settings:
    """读取 settings.json，不存在时返回默认值。

    老版本写下的 settings.json 没有 theme 字段时，按默认深色处理，向前兼容。
    """
    path = root / _CONFIG_FILE
    if not path.is_file():
        return Settings()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Settings(
            last_preset=data.get("last_preset", ""),
            window_width=int(data.get("window_width", 1100)),
            window_height=int(data.get("window_height", 750)),
            model_dir=data.get("model_dir", "E:\\ai\\ninfer-4090"),
            exe_path=data.get("exe_path", ""),
            params=data.get("params", {}),
            theme=_norm_theme(data.get("theme", THEME_DARK)),
        )
    except (json.JSONDecodeError, OSError, ValueError):
        return Settings()


def save_settings(root: Path, settings: Settings) -> None:
    """原子写入 settings.json。"""
    root.mkdir(parents=True, exist_ok=True)
    path = root / _CONFIG_FILE
    data = {
        "last_preset": settings.last_preset,
        "window_width": settings.window_width,
        "window_height": settings.window_height,
        "model_dir": settings.model_dir,
        "exe_path": settings.exe_path,
        "params": settings.params,
        "theme": settings.theme,
    }
    text = json.dumps(data, ensure_ascii=False, indent=2)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def list_presets(root: Path) -> list[str]:
    """列出 presets/ 目录下所有 .json 预设文件名（不含扩展名）。"""
    presets_dir = root / _PRESETS_DIR
    if not presets_dir.is_dir():
        return []
    names = []
    for f in sorted(presets_dir.glob("*.json")):
        names.append(f.stem)
    return names


def load_preset(root: Path, name: str) -> dict[str, Any] | None:
    """读取一个预设文件。不存在或损坏时返回 None。"""
    path = root / _PRESETS_DIR / f"{name}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_preset(root: Path, name: str, data: dict[str, Any]) -> None:
    """原子写入一个预设文件。"""
    presets_dir = root / _PRESETS_DIR
    presets_dir.mkdir(parents=True, exist_ok=True)
    path = presets_dir / f"{name}.json"
    text = json.dumps(data, ensure_ascii=False, indent=2)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def delete_preset(root: Path, name: str) -> bool:
    """删除一个预设文件。返回是否成功。"""
    path = root / _PRESETS_DIR / f"{name}.json"
    if path.is_file():
        path.unlink()
        return True
    return False


# Windows 文件名不允许出现的字符（预设名要落成 presets/<name>.json 的 stem）。
_ILLEGAL_PRESET_NAME_CHARS = set('\\/:*?"<>|')

# Windows 保留设备名（取扩展名前的首段，忽略大小写），命中即拒绝。
_RESERVED_PRESET_STEMS = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
)

# 预设名要落成文件名 stem，过长会触发文件名截断/报错，这里给个上限。
_MAX_PRESET_NAME_LEN = 50


def check_preset_name(name: str) -> str | None:
    """校验预设名能否安全地落成 presets/<name>.json。

    合法返回 None；否则返回一句可直接展示给用户的错误文案。
    这是「另存为 / 保存配置」落盘前的唯一守门：非法文件名字符、保留设备名、
    超长都会在这里被拦下并给出明确提示，避免 save_preset
    抛异常后界面静默无反馈（预设列表不刷新、使用者不知道到底存没存上）。
    """
    if not isinstance(name, str) or not name.strip():
        return "预设名称不能为空"
    name = name.strip()
    if len(name) > _MAX_PRESET_NAME_LEN:
        return f"预设名称过长（最多 {_MAX_PRESET_NAME_LEN} 个字符）"
    illegal = sorted({c for c in name if c in _ILLEGAL_PRESET_NAME_CHARS})
    if illegal:
        return f"预设名称含非法字符：{''.join(illegal)}（Windows 文件名不允许）"
    if name[-1] == ".":
        return "预设名称不能以「.」结尾（Windows 文件名不允许）"
    stem = name.split(".")[0].upper()
    if stem in _RESERVED_PRESET_STEMS:
        return "预设名称不能使用系统保留设备名（如 CON、NUL、COM1…）"
    return None


class ConfigStore:
    """配置与预设的统一存取入口。

    构造时必须传入 root（无无参构造，防止测试意外写入真实配置）。
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    def open_default(cls) -> "ConfigStore":
        return cls(resolve_config_root())

    def load_settings(self) -> Settings:
        return load_settings(self.root)

    def save_settings(self, settings: Settings) -> None:
        save_settings(self.root, settings)

    def list_presets(self) -> list[str]:
        return list_presets(self.root)

    def load_preset(self, name: str) -> dict[str, Any] | None:
        return load_preset(self.root, name)

    def save_preset(self, name: str, data: dict[str, Any]) -> None:
        save_preset(self.root, name, data)

    def delete_preset(self, name: str) -> bool:
        return delete_preset(self.root, name)

    def record_builtin_deletion(self, name: str) -> None:
        record_builtin_deletion(self.root, name)
