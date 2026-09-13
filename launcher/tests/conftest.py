"""跨测试文件共享的 pytest 夹具。

**唯一职责**：在整个测试会话开始前创建**一个**真正的 ``QApplication`` 实例（无头，
``QT_QPA_PLATFORM=offscreen``），且只创建这一次。

隐性契约（改动前务必读完）：

1. **必须是 ``QApplication``，不能是 ``QCoreApplication``**。``ui/widgets.py`` 的测试
   （``test_widgets.py``）要构造真实 ``QWidget`` 子类，这要求进程内存在 ``QApplication``
   实例；而 ``core/process.py`` 的测试（``test_process.py``）本身只需要 ``QCoreApplication``
   的事件循环，用 ``QCoreApplication.instance() or QCoreApplication([])`` 取实例——由于
   ``QApplication`` 是 ``QCoreApplication`` 的子类，``.instance()`` 会直接把这里创建好的
   ``QApplication`` 认作满足要求的实例并复用，不会另外再构造一个。
2. **顺序必须反过来才行得通，且 Qt 不允许先建 ``QCoreApplication`` 再建 ``QApplication``**
   （实测：会抛 ``Please destroy the QCoreApplication singleton before creating a new
   QApplication instance.``）。如果本文件不存在，两个测试模块各自的模块级夹具谁先跑
   由收集顺序决定（当前是文件名字母序，``test_process`` 先于 ``test_widgets``），
   跑到后一个时会撞上这条限制，且不是普通异常，是 Qt 的致命断言，会让整个 pytest 进程
   卡死或直接被杀，测试报告里不会有任何失败信息可看。这个 conftest 通过 session 级
   ``autouse`` 保证真正的 ``QApplication`` 总是最先建好，从根上避免这个撞车顺序。
3. **``QT_QPA_PLATFORM`` 用 ``setdefault``**，不覆盖外部已设置的值——本机若显式指定
   了别的平台插件（比如真的要跑一次带界面的手工调试），不应被这里的默认值悄悄改掉。

第二个职责（2026-08-30 新增）：**把 ``core/gpu_power.py`` 的三个顶层函数全局替换成假
实现**，见 :func:`_block_real_gpu_power`。

第三个职责（2026-09-12 新增）：**拦死所有模态对话框入口**，见 :func:`_block_modal_dialogs`。

4. **NVIDIA 驱动的读写必须在这里一次性拦掉，不能靠各个测试文件自己注入**。
   :class:`~ui.main_window.MainWindow` 不传 ``gpu_power_*`` 回调时会用真实实现
   （``ui/main_window.py`` 契约 10），于是每构造一次窗口就真的加载 ``nvapi64.dll``、
   建一次 DRS session。这有两个后果，都不会让任何断言变红：
   ① 慢——实测跑一批 UI 测试直接超过十分钟（每次构造窗口都要一次驱动往返）；
   ② 一旦哪个用例真的去点了那个开关，就会改掉跑测试这台机器的显卡全局设置，
   而驱动设置不在仓库里，没有任何一处 diff 会显示它被改过。
   靠「每个构造 MainWindow 的测试都记得注入三个假回调」是行不通的——漏一个就退回
   真实实现，而漏了的表现只是「有点慢」，没人会因此去查。所以在这里 ``autouse`` 拦死。
   需要验证真实驱动交互的用例请显式标 ``realsystem``（默认套件不跑，见 pytest.ini），
   并在用例内部自己恢复原函数。

5. **模态对话框必须在这里一次性拦掉，同样不能靠各个测试文件自己替换**。理由与契约 4
   同构，但后果更重：漏掉一个的表现不是「有点慢」，而是 ``pytest`` **永久挂起**——
   模态框在无人点击的测试进程里一直等下去，既不超时也不失败，``scripts/build.ps1``
   的 ``[2/8]`` 步骤就此停住，日志里没有任何线索指向是哪个用例。``realsystem`` 用例
   也不豁免：手工调试要弹真框，请在用例内部自己还原对应入口。
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from ninfer_launcher.core import gpu_power as gpu_power_module

#: ``core/gpu_power.py`` 那三个顶层函数的**原件**。在本模块顶层抓一次，
#: 此时 :func:`_block_real_gpu_power` 还没替换过任何东西，拿到的一定是真实现。
#: :func:`real_gpu_power_functions` 靠它把函数还原回去——``test_gpu_power.py`` 测的就是
#: 这三个函数本身，被替换掉之后那些用例是在测替身，会一路绿着什么也没验证。
_REAL_GPU_POWER_FUNCTIONS = {
    "describe_availability": gpu_power_module.describe_availability,
    "read_power_mode": gpu_power_module.read_power_mode,
    "apply_power_mode": gpu_power_module.apply_power_mode,
}


@pytest.fixture(scope="session", autouse=True)
def _shared_qapplication():
    """会话级唯一的 ``QApplication`` 实例（契约 1、2）。

    ``autouse``：不需要任何测试显式依赖它，只要跑了这个 ``tests/`` 目录下的任意用例，
    这个夹具就已经在最早的时机把实例建好。
    """
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _block_real_gpu_power(monkeypatch, request):
    """默认套件里一律不碰真实显卡驱动（契约 4）。

    替换的是 ``core.gpu_power`` 模块上的三个顶层函数：
    :func:`~core.gpu_power.describe_availability` / :func:`~core.gpu_power.read_power_mode`
    / :func:`~core.gpu_power.apply_power_mode`。
    :class:`~ui.gpu_power_switch.GpuPowerSwitch` 在构造时才把它们取成实例属性
    （``reader or read_power_mode``），所以打模块属性就能覆盖到全部未显式注入的调用方。

    假实现是**有状态的**（记住写进去的档位并让读取跟着变），不是恒定返回值：
    恒定返回值会让「开关点击后状态是否跟着变」这类用例即使实现坏了也照样通过。

    标了 ``realsystem`` 的用例不替换——那类用例的存在意义就是核对真实系统行为
    （pytest.ini 里默认排除，只在显式指定时才跑）。
    """
    if request.node.get_closest_marker("realsystem") is not None:
        yield None
        return

    state = {"mode": gpu_power_module.DISABLE_MODE}

    def fake_describe_availability():
        return gpu_power_module.PowerModeResult(
            ok=True, mode=None, message="（测试替身）NVIDIA 驱动接口可用"
        )

    def fake_read_power_mode():
        mode = state["mode"]
        return gpu_power_module.PowerModeResult(
            ok=True,
            mode=mode,
            message=f"（测试替身）当前电源管理模式：{mode.label}",
            setting_name=gpu_power_module.PREFERRED_PSTATE_SETTING_NAME,
        )

    def fake_apply_power_mode(enable):
        target = gpu_power_module.ENABLE_MODE if enable else gpu_power_module.DISABLE_MODE
        state["mode"] = target
        return gpu_power_module.PowerModeResult(
            ok=True,
            mode=target,
            message=f"（测试替身）电源管理模式已设为{target.label}",
            setting_name=gpu_power_module.PREFERRED_PSTATE_SETTING_NAME,
        )

    monkeypatch.setattr(gpu_power_module, "describe_availability", fake_describe_availability)
    monkeypatch.setattr(gpu_power_module, "read_power_mode", fake_read_power_mode)
    monkeypatch.setattr(gpu_power_module, "apply_power_mode", fake_apply_power_mode)
    yield state


@pytest.fixture
def real_gpu_power_functions(monkeypatch, _block_real_gpu_power):
    """把三个顶层函数还原成真实现，供 ``test_gpu_power.py`` 使用。

    还原**不等于会碰到显卡驱动**：那个文件里对 ``gpu_power._Nvapi`` 另外打了桩，
    真实现里所有实际的 NVAPI 调用都走不到。这个 fixture 要还原的只是「被测函数本身」，
    否则那些用例是拿替身当被测对象，断言全绿但什么都没验证。

    显式声明依赖 :func:`_block_real_gpu_power`，保证还原动作发生在替换之后——
    两个 fixture 都用同一个 ``monkeypatch``，顺序反了就是替换覆盖还原，
    而这个错误的表现同样是「测试全绿」。
    """
    for name, func in _REAL_GPU_POWER_FUNCTIONS.items():
        monkeypatch.setattr(gpu_power_module, name, func)
    yield


#: 会在无人操作的测试进程里永久阻塞的模态入口：``(类, 静态方法名元组)``。
#: 收录判据是「调用它会开一个等人操作的事件循环」，不是「产品当前有没有用到」——
#: 将来新写的调用点如果不在这张表里，漏掉的表现又会是挂死（见契约 5）。
_MODAL_STATIC_ENTRY_POINTS = (
    ("QMessageBox", ("question", "warning", "critical", "information", "about")),
    ("QFileDialog", ("getOpenFileName", "getSaveFileName", "getExistingDirectory")),
    ("QInputDialog", ("getText", "getInt", "getDouble", "getItem")),
)

#: :class:`~PySide6.QtWidgets.QDialog` 上的模态事件循环入口。``exec_`` 是 ``exec`` 的
#: 5.x 兼容别名，两者都要拦：产品代码用的是 ``exec_``（``ui/main_window.py`` 的
#: 「显示命令」对话框），而新代码更可能直接写 ``exec``。
_MODAL_DIALOG_EXEC_NAMES = ("exec", "exec_")


@pytest.fixture(autouse=True)
def _block_modal_dialogs(monkeypatch):
    """默认套件里一律不允许打开模态对话框（契约 5）。

    替换成的守卫**直接抛** :class:`AssertionError`，不是返回一个默认按钮。这一点是刻意的，
    理由与契约 4 里「假实现必须有状态」同源：给出默认返回值等于替被测代码把交互分支
    悄悄走通，用例即使在错误的时机弹了框也照样全绿，而模态框本身正是这个套件唯一
    「不产生任何失败信息就让整个进程停住」的失效模式，必须让它变红。

    需要覆盖交互路径的用例自己 ``monkeypatch`` 掉对应入口即可（``test_close_kill.py`` /
    ``test_delete_preset.py`` / ``test_show_command.py`` 已经这么做）。那些替换发生在
    本夹具之后——conftest 的 ``autouse`` 夹具比测试文件内的夹具先建立——所以是用例的
    替身覆盖本守卫，而不是相反；对应地，用例夹具的 ``monkeypatch`` 先还原、本守卫后
    还原，于是 ``window`` 这类夹具在 teardown 里调 ``close()`` 时仍受用例替身保护。

    实测背景（2026-09-12）：``test_external_service_sync.py`` 有一个用例把进程状态机推到
    ``RUNNING`` 后忘了复位，夹具 teardown 的 ``win.close()`` 于是走进
    ``MainWindow.closeEvent`` 的停止确认框，而该文件没有替换 ``QMessageBox``——
    ``pytest`` 就此永久挂起，``scripts/build.ps1`` 的 ``[2/8]`` 步骤停在原地不动，
    退出码、失败列表、超时报告一个都没有。靠「每个用例都记得复位状态」防不住这件事，
    漏掉的代价又是整条构建流水线卡死，所以在这里 ``autouse`` 拦死。
    """
    from PySide6.QtWidgets import QDialog, QFileDialog, QInputDialog, QMessageBox

    def make_guard(label):
        def _refuse_modal(*args, **kwargs):
            raise AssertionError(
                f"测试期间调用了模态对话框入口 {label}()。它会开一个等人操作的事件"
                "循环，在无人点击的测试进程里永久阻塞——整个套件挂死且不产生任何失败"
                "信息，所以默认拦死。\n"
                "若被测代码本不该走到交互分支：检查用例收尾时留下的状态，典型情形是"
                "把进程状态机推到运行态后没复位，夹具 teardown 的 close() 于是弹出"
                "停止确认框。\n"
                "若本用例确实要覆盖交互路径：在用例或其夹具里 monkeypatch 这个入口，"
                "给出确定的返回值。"
            )

        return _refuse_modal

    # 局部作用域里按名字取类，让 _MODAL_STATIC_ENTRY_POINTS 保持成一张可读的表
    owners = {"QMessageBox": QMessageBox, "QFileDialog": QFileDialog, "QInputDialog": QInputDialog}
    for owner_name, method_names in _MODAL_STATIC_ENTRY_POINTS:
        owner = owners[owner_name]
        for method_name in method_names:
            # raising=True（默认）：PySide6 换版本挪掉了某个入口就当场报错，
            # 而不是静默少拦一个——少拦的表现同样是挂死
            monkeypatch.setattr(
                owner, method_name, staticmethod(make_guard(f"{owner_name}.{method_name}"))
            )
    for exec_name in _MODAL_DIALOG_EXEC_NAMES:
        monkeypatch.setattr(QDialog, exec_name, make_guard(f"QDialog.{exec_name}"))
    yield
