# 关闭启动器时外部服务进程残留修复文档

> 文档日期：2026-09-13 · 范围：`launcher/`（PySide6 GUI）· 目标平台：Windows 11 / RTX 4090 24GB / 单卡
>
> 本文是**修复规格**，后续按本文逐节落地即可。问题为「直接关闭启动器后 `ninfer-serve.exe`
> 进程残留、继续占用显存」，根因定位与修复方案见下。

---

## 1. 问题现象

直接关闭启动器窗口后，`ninfer-serve.exe` 进程未被杀掉，残留在系统里继续占用约 23 GB 显存，
需用户手动到任务管理器结束。

---

## 2. 根因分析

### 2.1 涉及代码

- `ninfer_launcher/ui/main_window.py`
  - `closeEvent`（约第 869 行起）：窗口关闭时的停止判定与收尾。
  - 外部服务对账机制：`_reconcile_external_service` / `_stop_external`，成员
    `_external_state` / `_external_pid` / `_external_port` / `_external_owner` /
    `_external_stop_in_progress` / `_external_stopper`。
- `ninfer_launcher/core/process.py`
  - `ServerProcess.stop_and_wait()`：GUI 自身进程的同步停止升级链。
  - `ExternalProcessStopper`：外部实例的**异步**停止升级链（基于 QTimer）。
- `ninfer_launcher/core/process_control.py`
  - 纯函数升级链：`run_taskkill` → `terminate_process_hard`（Win32 TerminateProcess）
    → `process_terminated`（OpenProcess 探测的 OS 层真值）。
  - 显存回落等待：`VramSettleWatcher` / `settle_vram`。
  - 常量：`KILL_ESCALATION_SECONDS`、`KILL_CHECK_INTERVAL_MS`、`TERMINATE_GRACE_SECONDS`。

### 2.2 关闭判定的盲区

`closeEvent` 的停止判定只看 GUI 自己拉起的 `self._process`：

```python
if self._process.state.active or self._process.is_alive():
    # 二次确认 → self._process.stop_and_wait()
```

- `self._process.state` 是 GUI 自身 QProcess 的四态状态机；
- `self._process.is_alive()` 内部 `pid = self.pid or self._last_pid`，查的也只是
  **GUI 自身 QProcess 的 PID**。

而启动器另有一整套并行的「外部服务」跟踪机制：`_reconcile_external_service` 每秒对账一次，
识别 **CLI 启动 / 外部拉起、GUI 未持有 QProcess 句柄** 的 `ninfer-serve` 实例，记入
`_external_state` / `_external_pid`，停止时走 `ExternalProcessStopper`（`_stop_external`）。
`closeEvent` **完全没有纳入这套外部服务状态**。

### 2.3 残留触发场景

当面板显示的是一个**外部服务**（`_external_state == RUNNING`，而 `self._process.state == STOPPED`）时，
用户直接关闭窗口：

1. `self._process.state.active` → `False`（自身进程本就是 STOPPED）；
2. `self._process.is_alive()` → 查 GUI 自身 PID，也是 `False`；
3. → 整个 `if` 跳过，**既不弹确认框，也不触发任何停止流程**；
4. 窗口直接 `event.accept()` 关闭，外部 `ninfer-serve` 被彻底遗弃，继续占用显存。

这正对应用户描述的「直接关闭启动器后进程残留」。

### 2.4 已有加固为何没覆盖到

此前针对残留问题的加固——`tests/_repro_close_leak.py`、`tests/test_close_kill.py`、
`closeEvent` 里的 `or self._process.is_alive()` desync 兜底、`stop_and_wait` 的强杀升级链——
**全部只覆盖 GUI 自身进程**，从未覆盖外部实例这条路径。

> 次要观察：即便是自身进程场景，`closeEvent` 走的是同步 `stop_and_wait()`，逻辑完整
> （terminate → taskkill → 强杀升级 + OS 校验），在 `KILL_ESCALATION_SECONDS` 时限内基本能杀掉。
> 因此**最可能的残留触发条件是「外部 / CLI 启动的服务 + 直接关窗」**。

---

## 3. 修复目标

1. `closeEvent` 关闭判定纳入外部服务：面板显示外部服务在运行 / 加载中时，关闭也要弹二次确认。
2. 用户确认退出后，**同步**停掉外部实例（走与 `stop_and_wait` 同源的 taskkill → 强杀升级 + OS 校验），
   确认进程死亡后再放行窗口关闭。
3. 无 PID 可定位的外部服务（`/health` 通但无 PID 登记表）：与既有 `_stop_external` 行为一致，
   不阻断关闭，只落一条提示日志（无法代杀）。
4. 不破坏既有自身进程关闭链，不改动纯逻辑函数的语义。

---

## 4. 修复方案

### 4.1 核心难点

`ExternalProcessStopper` 是**异步**的（QTimer 驱动），而 `closeEvent` 需要**同步**等它杀完再
`event.accept()`。窗口一旦 `event.accept()`，事件循环随即收尾，异步 stopper 的 QTimer 不再有机会 tick。
因此不能在 `closeEvent` 里简单地 `_stop_external()` 后直接放行。

### 4.2 采纳方案：抽出同步外部停止函数（方案 A）

在 `core/process_control.py` 新增一个**纯同步**的外部实例停止函数，复用现有升级链纯函数，
不依赖 Qt 事件循环。签名建议：

```python
def stop_external_sync(
    pid: int,
    *,
    killer: Callable[[int], tuple[bool, str]] = run_taskkill,
    hard_terminator: Callable[[int], tuple[bool, str]] = terminate_process_hard,
    terminated_check: Callable[[int | None], bool] = process_terminated,
    kill_escalation: float = KILL_ESCALATION_SECONDS,
    kill_check_interval_ms: int = KILL_CHECK_INTERVAL_MS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    on_message: Callable[[str], None] | None = None,
) -> bool:
    """按 PID 同步停止外部实例，返回 True=已确认死亡，False=时限用尽仍存活。

    升级链与 ExternalProcessStopper 完全一致，仅把 QTimer 轮询换成阻塞 sleep：
    1. 已死 → 直接返回 True；
    2. run_taskkill 杀进程树；
    3. 仍存活 → 每 kill_check_interval_ms 重发 terminate_process_hard，
       以 process_terminated 校验，直到进程真死或 kill_escalation 用尽。
    全部 I/O 可注入，测试用假件替换后不碰真实进程。on_message 落日志（可选）。
    """
```

> 显存回落等待（`VramSettleWatcher` / `settle_vram`）在关闭路径上非必需——窗口即将销毁，
> 无需再等显存曲线回落。可省略以缩短关闭耗时；若要与自身进程关闭观感一致，也可在进程确认死亡后
> 追加一次 `settle_vram`（同步、可注入 sleep）。**默认省略**，仅在进程未能在时限内杀死时给显著警告。

`ExternalProcessStopper` 可后续重构为在其异步 tick 中复用同一升级语义（非本次必需，避免扩大改动面）。

### 4.3 `closeEvent` 改造

关闭判定纳入外部服务状态；用户确认后按「自身进程 / 外部实例」分别走对应的同步停止：

```python
def closeEvent(self, event) -> None:
    self_active = self._process.state.active or self._process.is_alive()
    external_active = self._external_state in (ServerState.RUNNING, ServerState.STARTING)

    if self_active or external_active:
        reply = QMessageBox.question(
            self, "确认退出",
            "服务器正在运行，退出将停止它。确定吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.No:
            event.ignore()
            return

        # 自身进程：走既有同步升级链（terminate → taskkill → 强杀升级 + OS 校验）
        if self_active:
            self._process.stop_and_wait()

        # 外部实例：同步停止（无 QProcess 句柄，从 taskkill 起步）
        if external_active:
            self._stop_external_blocking()

    # …既有收尾：保存窗口几何 / 参数、关性能模式、停预检定时器、control.shutdown、event.accept()
```

新增私有方法 `_stop_external_blocking`，封装取 PID、无 PID 时落提示日志、调用
`stop_external_sync`（把 stopper 的 `message` 落法沿用 `_log.append_line`）：

```python
def _stop_external_blocking(self) -> None:
    pid = self._external_pid
    if not pid:
        self._log.append_line(LogLine(
            LogSource.LAUNCHER,
            "外部服务在运行但无法定位其进程（无 PID 登记表），"
            "请在任务管理器 → 详细信息中结束占用该端口的进程",
        ))
        return
    owner = self._external_owner or "未知"
    self._log.append_line(LogLine(
        LogSource.LAUNCHER, f"关闭启动器：正在停止外部服务（PID {pid}，归属 {owner}）…"))
    ok = stop_external_sync(pid, on_message=lambda t: self._log.append_line(
        LogLine(LogSource.LAUNCHER, t)))
    if not ok:
        self._log.append_line(LogLine(
            LogSource.LAUNCHER,
            f"⚠ 外部服务进程（PID {pid}）在时限内未能杀死，将带着它退出，"
            "请在任务管理器中手动结束——它仍会占用显存"))
```

### 4.4 若已有异步外部停止流程正在进行

用户可能先点了「停止」（`_external_stop_in_progress == True`、`_external_stopper` 存活），
随后立刻关窗。此时 `closeEvent` 应：先停掉进行中的异步 stopper（`self._external_stopper` 的
定时器）避免与同步停止竞争，再按 PID 走一次 `stop_external_sync` 收敛到 OS 真值。
`external_active` 判定应把「正在停止外部」也视作需要收尾（用 `_external_stop_in_progress`
或 `_external_pid` 一并判断），避免关窗时因状态短暂切换而漏杀。

---

## 5. 判定矩阵（关闭时行为）

| 自身进程 | 外部服务观测 | 关闭时行为 |
|---|---|---|
| 运行 / 加载 / 停止中 | 任意（对账此时不覆盖面板） | 弹确认 → `stop_and_wait()`（既有链）|
| 停止 | RUNNING / STARTING（有 PID） | 弹确认 → `stop_external_sync(pid)`（新增同步链）|
| 停止 | RUNNING（`/health` 通但无 PID） | 弹确认 → 落提示日志，不代杀，正常关闭 |
| 停止 | 无外部服务 | 不弹确认，直接关闭（现状不变）|
| 停止 + OS 层仍存活（desync） | — | 既有 `or self._process.is_alive()` 兜底（现状不变）|

---

## 6. 测试方案

沿用可注入模式，不依赖真实 GPU / 真实 `ninfer-serve`（用 `ping.exe` 或假 killer 做替身）。

1. **纯函数 `stop_external_sync`**（`tests/test_process_control.py` 或新增文件）：
   - 进程已死 → 立即返回 `True`，不发 taskkill；
   - taskkill 后即死 → 返回 `True`；
   - taskkill 后仍活、若干轮强杀后死 → 返回 `True`，且 `terminated_check` 被多次调用；
   - 始终不死、时限用尽 → 返回 `False`，`on_message` 收到告警。
   - 全部用注入的假 `killer` / `hard_terminator` / `terminated_check` / `clock` / `sleep`。
2. **`closeEvent` 外部服务分支**（扩展 `tests/test_close_kill.py`）：
   - 构造 `_external_state = RUNNING`、`_external_pid` 指向一个 `ping` 替身进程，
     模拟 `closeEvent`（Yes）→ 断言替身进程被杀、窗口 `event.accept()`；
   - `_external_state = RUNNING` 但 `_external_pid = None` → 断言不阻断关闭、落提示日志；
   - `reply = No` → 断言 `event.ignore()`，替身进程仍活。
3. **回归保护**：既有自身进程关闭用例（`test_close_kill.py` 现有断言）保持全绿。

> 依据 `AGENTS.md`：新增 / 关键改动需补对应测试；仓库 84 个 CTest 全绿方可完成。
> （launcher 侧为 pytest 套件，随本仓库测试纪律执行。）

---

## 7. 落地清单

- [ ] `core/process_control.py`：新增 `stop_external_sync`，并加入 `__all__`。
- [ ] `core/process.py`：从 `process_control` 重导出 `stop_external_sync`（保持既有导入路径习惯）。
- [ ] `ui/main_window.py`：
  - [ ] `closeEvent` 判定纳入 `external_active`；
  - [ ] 新增 `_stop_external_blocking`；
  - [ ] 处理「异步外部停止进行中 + 关窗」的收敛。
- [ ] `tests/`：新增 `stop_external_sync` 纯函数用例 + 扩展 `test_close_kill.py` 外部分支用例。
- [ ] 运行 pytest 套件确认全绿。

---

## 8. 风险与边界

- **关闭耗时**：外部实例若在 GPU 驱动态拒绝终止，同步停止最坏会阻塞到 `KILL_ESCALATION_SECONDS`
  （当前 15 秒）。这与自身进程 `stop_and_wait` 的既有上限一致，属可接受代价（宁可多等，
  也不静默残留占 23 GB 显存的进程）。关闭期间窗口会短暂无响应，与既有 `stop_and_wait` 表现一致。
- **不代杀无 PID 的外部服务**：与仓库既有约束一致（`main_window` 只在能定位进程时动手），
  无 PID 登记表时仅提示，不猜测端口占用者。
- **不做运行时显存回落等待**：关闭路径省略 `settle_vram`，缩短关闭耗时；如需与自身进程观感一致
  可选加，非必需。
