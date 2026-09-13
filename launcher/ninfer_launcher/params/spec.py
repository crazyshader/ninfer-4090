"""参数元数据模型：参数种类、三态布尔值、单个参数的定义。

本模块只描述「一个参数长什么样」，不含任何具体参数定义（那是 ``registry`` 的职责），
也不含命令行组装逻辑（那是 ``builder`` 的职责）。
"""

from dataclasses import dataclass
from enum import Enum, auto
from typing import Any

__all__ = [
    "ParamKind",
    "Bool3",
    "EmitPolicy",
    "ParamSpec",
]


class ParamKind(Enum):
    """参数种类。决定落参规则与 UI 控件类型。"""

    INT = auto()         # 整数，始终落参
    FLOAT = auto()       # 浮点，始终落参
    ENUM = auto()        # 枚举单选，始终落参
    BOOL3 = auto()       # 三态布尔：不指定 / 开 / 关
    PATH = auto()        # 文件路径，空则不落参
    TEXT = auto()        # 字符串，空则不落参


class Bool3(Enum):
    """三态布尔值。

    - ``UNSET``：不落参
    - ``ON``：落 ``flag``
    - ``OFF``：落 ``off_flag``
    """

    UNSET = "unset"
    ON = "on"
    OFF = "off"

    @classmethod
    def from_config(cls, value: Any) -> "Bool3":
        """把原始值归一成 :class:`Bool3`。"""
        if isinstance(value, cls):
            return value
        if value is None:
            return cls.UNSET
        if isinstance(value, bool):
            return cls.ON if value else cls.OFF
        if isinstance(value, str):
            text = value.strip().lower()
            if text in ("", "unset", "none"):
                return cls.UNSET
            if text in ("on", "true", "1", "enabled"):
                return cls.ON
            if text in ("off", "false", "0", "disabled"):
                return cls.OFF
        raise ValueError(f"无法识别的三态布尔值：{value!r}")

    def to_config(self) -> str:
        """返回写入配置 json 的字符串形式。"""
        return self.value


class EmitPolicy(Enum):
    """落参策略。"""

    ALWAYS = auto()
    NON_EMPTY = auto()
    TRISTATE = auto()


_EMIT_POLICY_BY_KIND: dict[ParamKind, EmitPolicy] = {
    ParamKind.INT: EmitPolicy.ALWAYS,
    ParamKind.FLOAT: EmitPolicy.ALWAYS,
    ParamKind.ENUM: EmitPolicy.ALWAYS,
    ParamKind.BOOL3: EmitPolicy.TRISTATE,
    ParamKind.PATH: EmitPolicy.NON_EMPTY,
    ParamKind.TEXT: EmitPolicy.NON_EMPTY,
}


@dataclass(frozen=True)
class ParamSpec:
    """单个 ninfer-serve 参数的定义。"""

    key: str
    flag: str
    kind: ParamKind
    default: object
    label: str
    tab: str
    group: str
    tooltip: str
    choices: tuple = ()
    minimum: object = None
    maximum: object = None
    step: object = None
    off_flag: str | None = None
    off_value: str | None = None
    disabled_when: tuple = ()

    def __post_init__(self) -> None:
        if self.kind is ParamKind.ENUM and not self.choices:
            raise ValueError(f"{self.key}：ENUM 必须给出 choices")
        if self.kind is not ParamKind.BOOL3 and (self.off_flag or self.off_value):
            raise ValueError(f"{self.key}：off_flag / off_value 只对 BOOL3 有意义")
        if self.off_flag and self.off_value:
            raise ValueError(f"{self.key}：off_flag 与 off_value 互斥")

    @property
    def emit_policy(self) -> EmitPolicy:
        return _EMIT_POLICY_BY_KIND[self.kind]

    @property
    def has_off_form(self) -> bool:
        return bool(self.off_flag or self.off_value)