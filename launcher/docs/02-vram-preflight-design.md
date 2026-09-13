# 启动器显存预检（VRAM Preflight）实现文档

> 文档日期：2026-09-13 · 范围：`launcher/`（PySide6 GUI）· 目标平台：Windows 11 / RTX 4090 24GB / 单卡
>
> 本文是**实现规格**，后续按本文逐节落地即可。文中所有显存标定值来自对
> `qwen3_8_27b_8-19.ninfer` + `rk4v4-e8` + `mtp` + `vision` 组合的实测（见附录 A），
> 并对不同 `kv_dtype` / `spec` 给出换算规则。

---

## 1. 需求与目标

### 1.1 用户诉求

1. 启动 `ninfer-serve` 前，先估算目标配置（主要由 `max_context` / `kv_dtype` / `spec` / `vision` 决定）需要多少显存。
2. 若当前**空余显存不足**，提示用户**退出占用显存最多的程序**以腾出空间，并列出这些程序、各自占用、退出后可腾出多少。
3. 只有在**显存足够**时，启动器的「启动」按钮才可用；不足时禁用启动按钮并给出明确指引。

### 1.2 设计边界（明确不做的事）

- **不代替用户杀进程 / 释放别人的显存**。显存被哪个进程占用，只有该进程能释放；启动器只做「读数 + 估算 + 建议」，退不退由用户决定。这与仓库既有约束一致（`main_window` 停止外部服务也只在用户显式点「停止」后才动手）。
- **不做运行时动态扩容**。`max_context` 决定的 KV cache 是 `ninfer-serve` 启动时一次性预留的（见 `src/core/arena.cu` 的 `DeviceArena`、`src/runtime/engine/kv_capacity.cpp` 的 `resolve_kv_capacity`），预检只在启动前发生。
- **不追求字节级精确**。目标是「够 / 不够 + 差多少 + 退哪些程序能补上」的决策级准确度，预留安全余量吸收估算误差。

### 1.3 验收标准

- 空余显存足够时：「启动」可用，预检面板显示绿色「显存充足」。
- 不足时：「启动」禁用，面板显示红色「显存不足」+ 需再腾出的字节数 + 按占用降序排列的可退出进程列表（排除 ninfer 自身与系统关键进程）。
- 预检失败（NVML/nvidia-smi 均不可用、无法读进程）时：**不阻断启动**，降级为「无法预检，风险自负」的告警，「启动」保持可用（避免因监控故障把用户彻底锁死）。
- 全部逻辑有单元测试覆盖，且不依赖真实 GPU（沿用 `MonitorService` 的可注入模式）。

---

## 2. 总体架构

新增两个纯逻辑模块 + 一个 UI 组件 + 一处主窗口编排，全部落在 `launcher/ninfer_launcher/` 下，沿用 core 层「纯逻辑 / 纯编排 / 与系统打交道」三段分法（见 `core/monitor.py` 模块头注释）。

```
core/vram_estimate.py     纯逻辑：配置 → 显存需求字节数（VramRequirement）
core/gpu_processes.py     与系统打交道：枚举占用显存的进程（NVML 进程接口 + psutil 进程名）
core/vram_preflight.py    纯编排：读现状 + 估需求 + 列进程 → 预检结论（PreflightVerdict）
ui/preflight_panel.py     UI：展示结论 + 进程列表 + 刷新按钮（可选，见 §7）
ui/main_window.py         集成：启动前跑预检、门控「启动」按钮、周期性重算
```

数据流：

```
ParamsTab.get_values()  ─┐
ControlPanel.get_*()    ─┼─► vram_preflight.evaluate(config, snapshot, processes)
MonitorService.poll()   ─┤        │
gpu_processes.list_*()  ─┘        ▼
                            PreflightVerdict ──► main_window 门控启动按钮 + preflight_panel 展示
```

关键复用点：**不新开 NVML 句柄**。进程枚举复用 `MainWindow` 已持有的那一个 `MonitorService`（`main_window.py` 里 `monitor = MonitorService()` 是全局唯一实例，同时供监视面板、进程层显存回落、外部停止复用）。`gpu_processes` 接收注入的 NVML 模块，不自己 `import pynvml`。

---

## 3. 显存需求模型（`core/vram_estimate.py`）

### 3.1 显存构成

`ninfer-serve` 启动后常驻显存 = **权重** + **运行时预留**（runtime reservation），其中运行时预留又拆成「固定开销」与「随上下文线性增长的 KV」：

```
需求显存 ≈ 权重字节
         + 固定运行时开销（GDN state / workspace / graph allowance / persistent 等，与 max_context 基本无关）
         + max_context × 每 token KV 字节（随上下文线性）
         + 安全余量
```

这一拆分对应 C++ 侧 `SequenceCapacityCurve` 的仿射模型（`src/runtime/engine/kv_capacity.cpp`）：
`reservation = minimum_device_reservation_bytes + (pages − min_pages) × bytes_per_additional_main_page_group`，
其中 `bytes_per_additional_main_page_group` 即「每 64 token 的边际 KV 成本」，其余项归入固定开销。

### 3.2 标定常量（实测，见附录 A）

以 `qwen3_8_27b_8-19.ninfer` 为基准，在 `rk4v4-e8` + `mtp(draft=7)` + `vision` 下实测：

| 量 | 实测值 | 说明 |
|----|--------|------|
| 权重 H2D | 16.95 GiB | groupwise-int 量化常驻权重 |
| 固定运行时开销 | ≈ 1.15 GiB | runtime reservation 减去 KV 线性部分后的余量（GDN 293.62 MiB + workspace 433.25 MiB + persistent 其余 + graph） |
| 每 token KV（rk4v4-e8, mtp） | ≈ 18.1 KiB | text-kv + mtp-kv 的线性部分：(2.66 GiB + 0.17 GiB)/163840 ≈ 18.1 KiB |
| DWM/桌面显示地板 | 512 MiB | 与 C++ `--wddm-evictable-budget` 的 `kMinDwmHeadroom` 对齐 |

这些值集中定义为模块级常量，便于日后校准：

```python
# core/vram_estimate.py
MIB = 1024 * 1024
GIB = 1024 ** 3

#: 基准模型权重字节数（qwen3.8-27b groupwise-int，实测 16.95 GiB）。
#: 仅当无法从实测缓存拿到真实权重字节时作为兜底估算。
DEFAULT_WEIGHT_BYTES = int(16.95 * GIB)

#: 固定运行时开销（与 max_context 无关的那部分 reservation）。
FIXED_RUNTIME_BYTES = int(1.15 * GIB)

#: 每 token 的 KV 字节，按 kv_dtype × 是否启用 mtp 分档（见 §3.3）。
```

### 3.3 每 token KV 字节的分档

KV 字节随 `kv_dtype` 变化（4-bit E8 约为 int8 的一半、BF16 的 1/4，见 `src/targets/qwen3_6/impl/state/decoder_state.cpp` 的 plane 结构分析），并在启用 `mtp` 时增加约 6% 的 MTP KV 层开销：

```python
#: 每 token 的 text-KV 字节（单请求 / 并发=1），按 kv_dtype 分档。
#: 基准：rk4v4-e8 实测 text-kv 17.0 KiB/token；其余按 plane 宽度比例换算。
_KV_BYTES_PER_TOKEN = {
    "bf16":     64 * 1024,      # K/V 各 head_dim×2B，无量化
    "int8":     33 * 1024,      # K/V int8 + fp16 scale
    "rk8v4":    25 * 1024,      # 8-bit K + 4-bit V
    "rk4v4":    17 * 1024,      # 4-bit K + 4-bit V
    "rk4v4-e8": 17 * 1024,      # 4-bit E8 K + 4-bit V（实测基准）
    "rk2v4-e8": 13 * 1024,      # 2-bit E8 K + 4-bit V
}

#: 启用 mtp 时，KV 需求的乘数（MTP 额外一层 + draft window 页，实测约 +6%）。
_MTP_KV_MULTIPLIER = 1.06
```

> 注：`bf16`/`int8`/`rk8v4`/`rk4v4`/`rk2v4-e8` 的值目前是按 plane 宽度比例推算的估算档，只有 `rk4v4-e8` 经实测。实现时以 `rk4v4-e8` 为准，其余档在附录 B 的校准清单中标注为「待实测」。安全余量足够吸收这一档误差。

### 3.4 安全余量策略

```python
#: 安全余量：max(绝对下限, 需求的百分比)。吸收估算误差 + 运行时请求瞬态 + prompt-cache 快照。
SAFETY_FLOOR_BYTES = int(1.0 * GIB)
SAFETY_FRACTION = 0.05
```

`safety = max(SAFETY_FLOOR_BYTES, int(base_requirement * SAFETY_FRACTION))`。

> 依据：200K 上下文实测启动后剩 1.07 GiB 可稳定推理，224K 剩 590 MiB 偏危险（附录 A）。1 GiB 下限即取自这一观察。

### 3.5 公共接口

```python
@dataclass(frozen=True)
class VramConfig:
    """从 launcher 参数中抽取出、影响显存的字段。"""
    max_context: int
    kv_dtype: str
    spec: str                 # "none" | "mtp"
    vision: bool
    weight_bytes: int | None = None   # 已知真实权重字节（来自实测缓存）时传入，否则用默认

@dataclass(frozen=True)
class VramRequirement:
    weight_bytes: int
    fixed_runtime_bytes: int
    kv_bytes: int
    safety_bytes: int
    @property
    def total_bytes(self) -> int:
        return self.weight_bytes + self.fixed_runtime_bytes + self.kv_bytes + self.safety_bytes

def estimate_requirement(config: VramConfig) -> VramRequirement:
    """纯函数：配置 → 显存需求。无副作用、不碰系统。"""

def config_from_values(values: Mapping[str, Any], weight_bytes: int | None = None) -> VramConfig:
    """从 ParamsTab.get_values() 的结果 + 已知权重字节，构造 VramConfig。
    需处理 Bool3（vision/spec）到 bool/str 的归一，参照 params/builder.py 的 _param_value_to_config。"""
```

**纯函数、无副作用、可完全单测**，是测试主战场（对齐 `monitor.py` 的分层原则）。

---

## 4. 占用显存的进程枚举（`core/gpu_processes.py`）

### 4.1 数据来源

- **NVML 进程接口**：`nvmlDeviceGetComputeRunningProcesses(handle)` + `nvmlDeviceGetGraphicsRunningProcesses(handle)`，返回 `[(pid, usedGpuMemory), ...]`。合并去重（同一 PID 取两者较大值）。注意 `usedGpuMemory` 可能为 `None`（NVML 在部分驱动/权限下报不出单进程显存），此时记为 `None`，展示为「未知」。
- **进程名**：用 `psutil.Process(pid).name()` / `.exe()`；拿不到（进程已退出 / 权限不足）时回落到 `f"PID {pid}"`。

nvidia-smi 也能出进程级显存（`--query-compute-apps=pid,used_memory,process_name`），作为 NVML 不可用时的回落来源（可选，见 §4.4）。

### 4.2 数据结构

```python
@dataclass(frozen=True)
class GpuProcess:
    pid: int
    name: str                     # 进程名或 exe basename；拿不到时 "PID <pid>"
    used_bytes: int | None        # 该进程占用显存；NVML 报不出时 None
    is_self: bool = False         # 是否是 ninfer 自己（ninfer-serve.exe / ninfer.exe）
    is_protected: bool = False    # 是否系统关键进程（不建议用户退出，见 §4.3）
```

### 4.3 过滤与保护名单

- **排除自身**：进程名匹配 `ninfer-serve.exe` / `ninfer.exe`（大小写不敏感）标记 `is_self=True`，不出现在「建议退出」列表里（退掉自己没意义）。
- **保护名单**：`dwm.exe`、`csrss.exe`、`winlogon.exe`、`explorer.exe`、`System`、`Registry` 等系统进程标记 `is_protected=True`，**不建议退出**（退掉会导致桌面异常）。名单定义为模块常量 `_PROTECTED_NAMES: frozenset[str]`。
- 保护进程仍计入「已占用显存」总量，只是不进入可退出建议列表。

### 4.4 接口

```python
def list_gpu_processes(
    *,
    nvml_module: Any = _NVML_AUTO,        # 注入点，默认 import pynvml
    psutil_module: Any = _PSUTIL_AUTO,    # 注入点，默认 import psutil
    device_index: int = 0,
    self_names: frozenset[str] = _SELF_NAMES,
) -> tuple[GpuProcess, ...]:
    """枚举占用显存的进程，按 used_bytes 降序（None 排最后）。永不抛异常，失败返回空元组。"""
```

**永不抛异常**（对齐 `monitor.py` 契约 6：挂在界面周期任务上，抛异常等于点一下就崩）。所有 NVML / psutil 调用各自 try/except，单项失败不牵连整体。

### 4.5 复用现有 MonitorService 的 NVML 句柄

`MonitorService` 已持有解析好的 NVML 模块（`_resolved_nvml`），但目前是私有属性。实现时给 `MonitorService` 增加一个只读访问器，避免二次 `nvmlInit`：

```python
# core/monitor.py 新增
@property
def nvml_module(self) -> Any:
    """已解析并初始化成功的 NVML 模块；未走 NVML 或尚未 poll 时为 None。
    供 gpu_processes 复用同一 NVML 句柄，不重复 nvmlInit。"""
    return self._resolved_nvml if self._backend is MonitorSource.NVML else None
```

`gpu_processes.list_gpu_processes` 的 `nvml_module` 参数即接收它；为 `None` 时自行 `import pynvml`（覆盖 monitor 走了 nvidia-smi 回落、但进程枚举仍想试 NVML 的情形）。

---

## 5. 预检编排（`core/vram_preflight.py`）

### 5.1 结论数据结构

```python
class PreflightStatus(Enum):
    OK = "ok"                    # 显存充足，可启动
    INSUFFICIENT = "insufficient"# 显存不足，禁用启动
    UNAVAILABLE = "unavailable"  # 无法预检（读不到显存/进程），不阻断启动

@dataclass(frozen=True)
class PreflightVerdict:
    status: PreflightStatus
    requirement: VramRequirement          # 估算需求
    free_bytes: int | None                # 当前空余显存
    total_bytes: int | None               # 显存总量
    shortfall_bytes: int                  # 还需腾出的字节（OK 时为 0）
    candidates: tuple[GpuProcess, ...]    # 建议退出的进程（降序，已排除 self/protected）
    messages: tuple[str, ...] = ()        # 面向用户的说明

    @property
    def can_start(self) -> bool:
        # 不足时禁止；OK 与 UNAVAILABLE 都放行（UNAVAILABLE 降级为告警不阻断）
        return self.status is not PreflightStatus.INSUFFICIENT
```

### 5.2 判定逻辑

```python
def evaluate(
    config: VramConfig,
    gpu: GpuSnapshot | None,               # 来自 MonitorService.poll().gpu_by_index(0)
    processes: tuple[GpuProcess, ...],
) -> PreflightVerdict:
    req = estimate_requirement(config)
    if gpu is None or gpu.mem_total_bytes is None or gpu.mem_used_bytes is None:
        return PreflightVerdict(UNAVAILABLE, req, None, None, 0, (), 
                                ("无法读取显存占用，未做预检；启动风险自负",))
    free = gpu.mem_total_bytes - gpu.mem_used_bytes
    if free >= req.total_bytes:
        return PreflightVerdict(OK, req, free, gpu.mem_total_bytes, 0, (), ...)
    shortfall = req.total_bytes - free
    candidates = tuple(p for p in processes if not p.is_self and not p.is_protected
                       and p.used_bytes)                     # 有明确占用、可退出的
    return PreflightVerdict(INSUFFICIENT, req, free, gpu.mem_total_bytes, shortfall, candidates, ...)
```

### 5.3 「退出后能否满足」的提示

在 `messages` 里给出可操作指引，例如：

- 「目标需要 20.1 GiB，当前空余 6.5 GiB，还差 13.6 GiB。」
- 「占用最多：chrome.exe 1.8 GiB、Code.exe 0.9 GiB、WeChat.exe 0.6 GiB。」
- 累加候选进程的 `used_bytes`，从占用最多的开始，算出「退出前 N 个可腾出 X GiB，可满足 / 仍差 Y」。

累加算法（纯逻辑，单测覆盖）：

```python
def processes_to_close(candidates, shortfall) -> tuple[tuple[GpuProcess, ...], int]:
    """从占用最多的开始累加，返回 (足以覆盖 shortfall 的最小进程集合, 覆盖后仍缺的字节)。
    若全退光仍不够，返回全部候选 + 剩余缺口（提示用户即使全退也不够，需降低 max_context）。"""
```

当「即使退光所有可退进程也不够」时，`messages` 追加建议：调低 `max_context`、或换更省显存的 `kv_dtype`（如 `rk2v4-e8`）、或关闭 `vision`。

---

## 6. 主窗口集成（`ui/main_window.py`）

### 6.1 触发时机

1. **参数变化时**：`ParamsTab` 的 `valueChanged` 信号、`ControlPanel` 的 `model_changed`（影响权重字节）触发一次重算。
2. **周期性**：显存占用是动态的（用户退了程序后应自动解禁「启动」）。挂一个 `QTimer`（复用监视面板的 1s 节奏，或独立 2~3s 定时器）周期重算。
3. **点「启动」前**：`_on_start` 里最后再跑一次预检，作为兜底硬门控（见 §6.3）。

### 6.2 「启动」按钮门控

现状：`ControlPanel.apply_state(STOPPED)` 里 `_btn_start.setEnabled(True)`。改为叠加预检结论——**启动按钮可用 = 状态机允许（STOPPED）AND 预检放行（can_start）**。

实现方式（不破坏现有状态机）：
- 给 `ControlPanel` 增加 `set_preflight_blocked(blocked: bool, reason: str)` 方法，内部记 `_preflight_blocked` 标志。
- `apply_state` 在 STOPPED 分支里改为 `self._btn_start.setEnabled(not self._preflight_blocked)`，并在 blocked 时设 `setToolTip(reason)`。
- `set_preflight_blocked` 若当前是 STOPPED 态，立即刷新按钮可用性；非 STOPPED 态只记标志（下次回到 STOPPED 再生效）。
- `main_window` 在每次预检后调用 `self._control.set_preflight_blocked(not verdict.can_start, reason)`。

> 契约补充写进 `control_panel.apply_state` 的 docstring：「启动」可用 = STOPPED 且未被预检阻断。

### 6.3 `_on_start` 的硬门控

在 `_on_start` 现有校验链（`validate_values` → 模型存在 → 端口检查）之后、`build(values)` 之前，插入预检兜底：

```python
verdict = self._run_preflight()   # 复用编排，读当前实时显存
if verdict.status is PreflightStatus.INSUFFICIENT:
    # 理论上此时按钮已被禁用；这里是防御性兜底（周期定时器与点击之间的竞态）
    QMessageBox.warning(self, "显存不足", self._format_preflight_message(verdict))
    return
```

`UNAVAILABLE` 不拦截（降级告警，见 §1.3）。

### 6.4 权重字节的获取

`VramConfig.weight_bytes` 优先用**实测缓存**（更准）：
- `ninfer-serve` 启动成功后，日志里有 `weight H2D 16.95 GiB` / `load weights 100.00% 16.95 GiB`。可在 `_on_log_line` 里解析这行，按「模型文件路径 → 权重字节」缓存到 `settings.json`（新增字段 `weight_bytes_cache: dict[str, int]`）。
- 下次对同一模型预检时，用缓存值；无缓存时用 `DEFAULT_WEIGHT_BYTES`（16.95 GiB）兜底。

这样第一次凭标定值估算，跑过一次后转为该模型的真实值，换模型也能自适应。日志解析放在 `core/` 的一个小纯函数里（`parse_weight_bytes_from_log(line) -> int | None`）便于单测。

---

## 7. 预检 UI（`ui/preflight_panel.py`，可选但推荐）

在「控制」页的右列顶部（资源监视面板上方），展示预检结论：

- 状态标识：绿「显存充足」/ 红「显存不足」/ 灰「无法预检」。
- 一行摘要：`需求 20.1 GiB · 空余 6.5 GiB · 缺口 13.6 GiB`。
- 不足时展开进程列表（名称 + 占用，降序），并高亮「退出这些可腾出 X GiB」的建议集合。
- 一个「刷新」按钮（手动重算，不必等定时器）。

UI 组件只接收 `PreflightVerdict` 渲染，不做任何估算/枚举（对齐 `monitor_panel` 只渲染 `SystemSnapshot` 的风格）。若为控制工期，MVP 阶段可先不做独立面板，只用 `_btn_start` 的 tooltip + 不足时的 `QMessageBox` 承载信息，§7 面板作为增强项。

---

## 8. 依赖

- **psutil**：`monitor.py` 已用（`requirements.txt` 应已含）。进程名枚举依赖它，确认其在 `requirements.txt` 中。
- **pynvml (nvidia-ml-py)**：`monitor.py` 已可选依赖。进程级显存接口 `nvmlDeviceGetComputeRunningProcesses` 在 pynvml 中可用。二者缺失时预检降级为 `UNAVAILABLE`，不阻断启动。

---

## 9. 测试计划（`tests/`，不依赖真实 GPU）

沿用 `test_monitor.py` 的可注入 Fake 模式（FakeNvml / fake psutil / 假 snapshot）。

| 测试文件 | 覆盖点 |
|----------|--------|
| `test_vram_estimate.py` | `estimate_requirement`：各 kv_dtype 分档、mtp 乘数、vision、权重字节覆盖、安全余量 floor/fraction 取大；`config_from_values` 对 Bool3 的归一 |
| `test_gpu_processes.py` | `list_gpu_processes`：NVML compute+graphics 合并去重、usedGpuMemory=None、psutil 拿不到名字回落、self/protected 过滤、降序排序、NVML/psutil 缺失返回空元组不抛 |
| `test_vram_preflight.py` | `evaluate`：free≥need→OK、free<need→INSUFFICIENT+shortfall+candidates、gpu=None→UNAVAILABLE；`processes_to_close` 累加算法（够/全退仍不够）；`can_start` 三态 |
| `test_preflight_gating.py` | `ControlPanel.set_preflight_blocked` + `apply_state(STOPPED)` 联动：blocked 时启动禁用、tooltip 正确、解除后恢复；`_on_start` 兜底拦截（用假 verdict 注入） |
| `test_weight_cache.py` | `parse_weight_bytes_from_log` 解析日志行；缓存读写 settings.json |

断言粒度对齐既有测试：`test_control_buttons_state.py` 测按钮矩阵，`test_registry.py` 测参数默认值，风格一致。

---

## 10. 实现顺序（建议）

1. `core/vram_estimate.py` + `test_vram_estimate.py`（纯逻辑，无外部依赖，最先落地）。
2. `core/gpu_processes.py` + `MonitorService.nvml_module` 访问器 + `test_gpu_processes.py`。
3. `core/vram_preflight.py` + `test_vram_preflight.py`。
4. `ControlPanel.set_preflight_blocked` + `apply_state` 改造 + `test_preflight_gating.py`。
5. `main_window` 集成：定时器、参数变化触发、`_on_start` 兜底、权重缓存。
6. （增强）`ui/preflight_panel.py` + `test_preflight_panel_ui.py`。

每步独立可测、可合入，前 3 步不碰 UI，风险最低。

---

## 附录 A：实测标定数据（2026-09-13，RTX 4090 24GB）

模型 `qwen3_8_27b_8-19.ninfer`，`--kv-dtype rk4v4-e8 --spec mtp --draft-tokens 7 --lm-head-draft --reasoning-effort low --vision`：

| max_context | KV pages | runtime 预留 | text-kv | mtp-kv | gdn-state | workspace | 权重 H2D | 启动后剩余 | 结果 |
|-------------|----------|--------------|---------|--------|-----------|-----------|----------|-----------|------|
| 8192 (CLI, +wddm) | 128/128 | 1.27 GiB | 136 MiB | 8.57 MiB | 293.62 MiB | 433.25 MiB | 16.95 GiB | 0 B* | ✓ |
| 163840 | 2560/2560 | 3.95 GiB | 2.66 GiB | 170 MiB | 293.62 MiB | 433.25 MiB | 16.95 GiB | 1.76 GiB | ✓ |
| 204800 | 3200/3200 | 4.66 GiB | 3.32 GiB | 212.57 MiB | 293.62 MiB | 433.25 MiB | 16.95 GiB | 1.07 GiB | ✓ 推理正常 |
| 229376 | 3584/3584 | 5.08 GiB | 3.72 GiB | 238.07 MiB | 293.62 MiB | 433.25 MiB | 16.95 GiB | 590 MiB | ✓ 但余量危险 |

\* CLI auto 模式下 `free after startup` 归零是把 slack 全给了 KV 的表现，非真的耗尽。

推导：
- 每 token KV（text+mtp 线性部分）= (2.66 GiB + 0.17 GiB) / 163840 ≈ **18.1 KiB/token**，与 204800 档 (3.32+0.21)/204800 ≈ 18.1 KiB 一致，交叉验证通过。
- 固定开销 = runtime 预留 − KV 线性部分 ≈ 3.95 GiB − (163840×18.1KiB≈2.83 GiB) ≈ 1.12 GiB，取 **1.15 GiB**（含少量并发/graph 波动余量）。
- `free after weights`：开 `--wddm-evictable-budget` 时 6.54 GiB，不开时 5.55 GiB（差约 1 GiB 为桌面/后台占用）。预检读的是 NVML 实时 free，天然反映这一差异，无需额外建模。

## 附录 B：待实测校准清单

以下常量当前为推算值，建议后续用对应配置各实测一次并回填：

- `_KV_BYTES_PER_TOKEN` 中除 `rk4v4-e8` 外的 5 档（`bf16`/`int8`/`rk8v4`/`rk4v4`/`rk2v4-e8`）。
- `spec="none"`（关闭 MTP）时 `_MTP_KV_MULTIPLIER` 应为 1.0，且固定开销中 replay-records（13.64 MiB）消失——影响很小，可并入安全余量。
- 换其他受支持模型（若未来支持）时 `DEFAULT_WEIGHT_BYTES` 与固定开销需重新标定；权重字节已由 §6.4 的实测缓存自适应。
