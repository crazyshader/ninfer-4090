"""CLI 入口：UTF-8 强制 + argparse + 子命令分发 + 单行 JSON 输出 + 退出码。

输出契约（docs/01-ninfer-launcher-cli.md 第 6 节）：

- stdout 有且只有一行 JSON（调用方按 UTF-8 解码后 json.loads）；
- 进度 / 提示一律走 stderr；
- 退出码：0 = ok:true，1 = 业务失败或未预期异常，2 = 用法错误（argparse）。

强制 UTF-8（docs 陷阱 1）必须在**任何输出之前**执行：Windows 控制台默认 GBK
代码页，中文 message 不 reconfigure 就会变乱码甚至 UnicodeEncodeError。
"""

from __future__ import annotations

import argparse
import sys
import time

__all__ = [
    "PROG",
    "DEFAULT_HOST",
    "DEFAULT_TIMEOUT_SECONDS",
    "build_parser",
    "run",
    "main",
]

PROG = "ninfer-launcher-cli"
DEFAULT_HOST = "127.0.0.1"
#: 就绪等待硬上限（秒）。只是保险绳：调用方（dsh 插件）靠杀子进程中止等待，
#: 不依赖这个值（docs 第 4 节 --timeout 语义）。
DEFAULT_TIMEOUT_SECONDS = 600.0


def _force_utf8() -> None:
    """stdout / stderr 强制 UTF-8。必须在任何输出之前调用（docs 陷阱 1）。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            # 重定向到非真实文件对象等极端情形：保持原样，不因此让命令失败
            pass


def build_parser() -> argparse.ArgumentParser:
    """四个子命令的参数解析器（参数表见 docs 第 4 节）。"""
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="ninfer-serve 服务的无 GUI 命令行入口（status / start / stop / ensure）",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add_common(p: argparse.ArgumentParser, *, timeout: bool, force: bool) -> None:
        p.add_argument("--preset", default=None, metavar="NAME", help="用哪个预设的参数")
        p.add_argument("--exe", default=None, metavar="PATH", help="ninfer-serve.exe 路径")
        p.add_argument("--host", default=DEFAULT_HOST, metavar="HOST", help="健康检查主机（默认 127.0.0.1）")
        p.add_argument("--json", action="store_true", default=True, help=argparse.SUPPRESS)  # 恒为真，保留占位
        if timeout:
            p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS, metavar="SEC", help="等待就绪的上限秒数（默认 600）")
        if force:
            p.add_argument("--force", action="store_true", help="允许停止 owner 不是 cli 的实例")

    p_status = sub.add_parser("status", help="查询服务状态（无副作用）")
    add_common(p_status, timeout=False, force=False)
    p_status.add_argument("--tail", type=int, default=0, metavar="N", help="附带服务日志尾部 N 行（上限 200）")

    p_start = sub.add_parser("start", help="拉起服务并等到就绪（幂等）")
    add_common(p_start, timeout=True, force=False)

    p_stop = sub.add_parser("stop", help="停止服务并等显存回落（幂等）")
    add_common(p_stop, timeout=False, force=True)

    p_ensure = sub.add_parser("ensure", help="幂等地保证服务可用（插件唯一入口）")
    add_common(p_ensure, timeout=True, force=False)
    return parser


def _dispatch(args: argparse.Namespace) -> "object":
    """把解析后的参数分发给四个动作（默认值全部是真实实现）。"""
    from ..core import config
    from . import actions

    root = config.resolve_config_root()
    if args.command == "status":
        return actions.action_status(root=root, preset_name=args.preset, host=args.host, tail=args.tail)
    if args.command == "start":
        return actions.action_start(
            root=root, preset_name=args.preset, exe_path=args.exe, host=args.host, timeout=args.timeout,
        )
    if args.command == "stop":
        return actions.action_stop(root=root, preset_name=args.preset, host=args.host, force=args.force)
    if args.command == "ensure":
        return actions.action_ensure(
            root=root, preset_name=args.preset, exe_path=args.exe, host=args.host, timeout=args.timeout,
        )
    raise AssertionError("未知子命令：%r" % args.command)


def run(argv: list[str] | None = None) -> int:
    """完整执行一次 CLI 调用，返回进程退出码。永不抛异常（异常转 internal-error JSON）。"""
    _force_utf8()
    from .result import CliResult, ERR_INTERNAL, STATE_UNKNOWN

    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse 用法错误：退出码 2，无 JSON（调用方按退出码识别）
        return int(exc.code if isinstance(exc.code, int) else 2)

    started = time.monotonic()
    result: CliResult
    try:
        result = _dispatch(args)
    except Exception as exc:  # noqa: BLE001 —— 兜底：崩了也输出一行合法 JSON
        result = CliResult(
            action=args.command,
            ok=False,
            state=STATE_UNKNOWN,
            message="CLI 内部错误：%s: %s" % (type(exc).__name__, exc),
            error=ERR_INTERNAL,
        )
        import traceback

        try:
            sys.stderr.write(traceback.format_exc())
            sys.stderr.flush()
        except Exception:  # noqa: BLE001
            pass

    result.elapsed_ms = round((time.monotonic() - started) * 1000.0, 1)
    line = result.to_json()
    try:
        sys.stdout.write(line + chr(10))
        sys.stdout.flush()
    except Exception:  # noqa: BLE001 —— 最后手段：stdout 写不了就丢给 stderr
        try:
            sys.stderr.write("无法写出 JSON 结果：" + line + chr(10))
        except Exception:  # noqa: BLE001
            pass
    return 0 if result.ok else 1


def main() -> int:
    """python -m ninfer_launcher.cli 的入口。"""
    return run()


if __name__ == "__main__":
    sys.exit(main())
