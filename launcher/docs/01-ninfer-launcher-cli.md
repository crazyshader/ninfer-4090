# 实现 ninfer launcher 的 CLI 详细流程

> 本文档是 `ninfer-launcher` 新增命令行入口的实现说明，可独立照此实现。
>
> **实现位置**：`E:\ai\ninfer-4090-native\launcher\`（Python 仓库，与本文档不在同一个仓库）。
>
> **配套文档**：`02-dsh-plugin-auto-lifecycle.md`（dsh 插件侧）。两份文档共享同一份 CLI 契约，
> 契约本身以**本文档第 6 节**为唯一事实源；插件侧文档引用它，不复述。
>
> **事实标注**：文中对现有代码的引用（函数名、路径、常量）均已逐个核实；标注为
> 「待验证」的部分是推断，实现时必须先验证。

---

## 0. 为什么需要这个 CLI

dsh 使用本地模型时，模型服务（`ninfer-serve.exe`）必须已经在跑。目前拉起它的唯一方式是
手动打开 launcher GUI 点「启动」。目标是让 dsh 插件能程序化地拉起和停止它，从而实现
「用的时候自动启动、长时间不用自动停止释放显存」。

为什么不让插件自己 spawn `ninfer-serve.exe`：launcher 的 `core/process.py` 里那套
**三级强杀升级链**是为 Windows 上 CUDA 进程「处于 GPU 驱动临界区时拒绝终止」这个真实
问题写的，已经在真实场景验证过（`tests/test_close_kill.py` 用真进程做了三个场景的回归）。
在插件里用 TypeScript 重写一遍这套逻辑，等于把已经踩平的坑重新踩一次。

---

## 1. 目标与边界

### 要做的

给 launcher 增加一个**无 GUI 的命令行入口**，提供四个动作：

| 动作 | 用途 |
|---|---|
| `status` | 查询服务当前状态，无副作用 |
| `start` | 拉起服务并等到就绪 |
| `stop` | 停止服务并等显存回落 |
| `ensure` | 幂等地「保证服务可用」——插件唯一调用的入口 |

### 明确不做的

- **不做 GUI 的替代品**。参数编辑、预设管理、资源监视仍然只在 GUI 里。
- **不碰 NVAPI 电源管理模式**。GUI 的语义是「用户手动开、程序负责关」；CLI 是无人值守
  路径，没有「用户手动」这个前提，擅自开会让显卡在用户不知情时锁在最高性能档。若将来
  要支持，做成显式 `--performance-mode on` 并在 `stop` 时对称关闭。
- **不写 `settings.json`**。尤其不写 `last_preset`——否则用户下次打开 GUI 会发现预设被
  悄悄换过，而且没有任何迹象说明是谁改的。CLI 只读配置，不改配置。
- **不做资源监视轮询**。只在 `stop` 的显存回落等待里用一次 `MonitorService`，用完立即
  `shutdown()` 释放 NVML。

---

## 2. 四条核心设计约束

### 约束 1：零 Qt 依赖

CLI 全程不 import PySide6。这不是洁癖，是两个实际收益：进程启动快（不用初始化 Qt），
以及不需要跑事件循环（`QProcess` / `QTimer` 都要求事件循环，在一次性命令里是纯累赘）。

需要复用的能力全部是纯函数，**但「函数不依赖 Qt」和「导入它不拖 Qt」是两件事**：
`core/health.py` 与 `core/process.py` 的**模块顶层就 `import PySide6`**（为了
`HealthPoller` / `ServerProcess`），所以 `from core.health import probe_health` 照样会把
整个 Qt 加载进来——纯函数待在一个不纯的模块里，等于不可用。

因此这些纯逻辑要先**下移到独立模块**，原模块 re-export 保持 GUI 与既有测试的导入路径不变：

| 原位置（顶层 import Qt） | 下移后的纯模块 | 内容 |
|---|---|---|
| `core/health.py` | **`core/health_probe.py`** | `HealthState` / `HealthResult` / `classify_health` / `probe_health` / `HEALTH_*` 常量 |
| `core/process.py` | **`core/process_control.py`** | `run_taskkill` / `terminate_process_hard` / `process_terminated` / `settle_vram` / `VramSettleWatcher` / `decode_output` / `KILL_*`、`TAIL_LINE_COUNT` 等常量 |

原模块只保留绑 Qt 的那部分（`HealthPoller` / `ServerProcess`），并**原样重导出全部纯逻辑
符号**（用显式 `__all__`，并在文件头注释写明「这些看似无用的 re-export 是为了不破坏既有
导入路径」——否则会被当成冗余导入删掉）。

下移之后的完整复用清单：

| 能力 | 位置 | Qt 依赖 |
|---|---|---|
| 配置根解析 | `core/config.py` → `resolve_config_root()` | 无 |
| settings 读取 | `core/config.py` → `load_settings(root)` | 无 |
| 预设读取 | `core/config.py` → `list_presets(root)` / `load_preset(root, name)` | 无 |
| 项目根探测 | `core/config.py` → `find_project_root()`（见第 4 节的迁移说明） | 无 |
| 参数校验 | `params/registry.py:174` → `validate_values(values)` | 无 |
| 命令行组装 | `params/builder.py` → `build(values)` | 无 |
| 端口占用探测 | `core/ports.py` → `check_port(port)` | 无（socket） |
| 就绪探测 | **`core/health_probe.py`** → `probe_health(host, port)` / `classify_health(code, body)` | 无（urllib） |
| 杀进程树 | **`core/process_control.py`** → `run_taskkill(pid, timeout)` | 无（subprocess） |
| Win32 强杀 | **`core/process_control.py`** → `terminate_process_hard(pid)` | 无（ctypes） |
| 进程存活探测 | **`core/process_control.py`** → `process_terminated(pid)` | 无（OpenProcess） |
| 显存回落等待 | **`core/process_control.py`** → `settle_vram(...)` / `VramSettleWatcher` | 无（全部依赖可注入） |
| 显存读数 | `core/monitor.py` → `MonitorService` / `make_vram_reader(service)` | 无 |
| 输出解码 | **`core/process_control.py`** → `decode_output(data)` | 无 |

> **隐性契约**：`cli/` 只许 import `core` 的上述纯模块和 `params`。三条禁令：
> 1. **禁止 import `ui` 下任何模块**——直接把 PySide6 拖进来；
> 2. **禁止 import `core.health` 和 `core.process`**——这两个模块顶层 import 了 Qt，
>    要的纯逻辑在 `core.health_probe` / `core.process_control` 里；
> 3. 违反前两条**都不会报错、不会让测试变红**，只会让 CLI 悄悄变慢好几百毫秒（打包版
>    因 spec 的 `excludes PySide6` 会在运行期 import 失败）。
>
> 必须写在 `cli/__init__.py` 顶部的模块级注释里，并**补一条断言 CLI 包不拖 Qt 的测试**
> （见第 10 节）——把纪律变成自动关卡，比靠人记住可靠。

### 约束 2：fire-and-forget

`start` 拉起服务后 CLI **自己退出**，服务进程留在后台独立存活。

原因有三条，第三条最实际：

1. Windows 上父子进程的终止信号传递不可靠，让 CLI 常驻并持有服务反而在 CLI 被杀时更
   容易留下残留。
2. 调用方只需要「调命令、读 JSON」，最容易测试。
3. 加载 27B 权重要几十秒到一分钟。服务与调用方解耦，意味着调用方重启不必重新加载权重。

代价：服务进程没有活着的父进程看着它，`stop` 必须靠**落盘的 PID 文件**定位。launcher
现在只有内存里的 `ServerProcess.last_pid`，配置根下没有任何 PID 文件——这是要新增的东西
（第 7 节）。

### 约束 3：单行 JSON 到 stdout

调用方是程序，不是人。所以：

- **stdout 只输出一行 JSON**，无论成功失败。
- 人类可读的进度信息（「正在等待就绪…」）一律走 **stderr**，不污染 stdout。
- 入口处强制 UTF-8：`sys.stdout.reconfigure(encoding='utf-8')`。Windows 控制台默认
  代码页是 GBK，中文 `message` 会变成乱码，最坏情况直接 `UnicodeEncodeError` 让整个
  命令失败。这一条**必须在任何输出之前**执行。

### 约束 4：状态真值顺序

判定「服务现在是什么状态」时，三个信息源的可信度**严格分级**，顺序不能反：

1. **`/health` 探测**（`probe_health`）—— 唯一权威的「服务能用」判据。
2. **`process_terminated(pid)` 的 OS 探测** —— 判「进程还在不在」的真值。
3. **PID 文件** —— 只用来定位 PID 和归属，**绝不作为状态真值**。

这个顺序沿用 `process.py` 的既有原则（「一切『进程已消失』的判定都以 OS 层
`OpenProcess` 探测为准」）。PID 文件会因为断电、强杀、手工删除而与现实脱节，把它当真值
会得出「服务在跑」但实际什么都没有的结论。

组合出的状态：

| `/health` | 进程 | 判定 |
|---|---|---|
| 200 | — | `running`（不必再查进程，能服务就是能服务） |
| 503 | 活着 | `starting`（正在加载权重） |
| 连不上 | 活着 | `starting`（进程起来了还没 listen） |
| 连不上 | 不在 | `stopped` |
| 200 | 无 PID 文件 | `running`，`owner: "external"`（GUI 或手工启的） |

---

## 3. 目录与模块划分

```
launcher/ninfer_launcher/cli/
├── __init__.py       模块级契约注释（禁止 import ui）+ 版本号
├── __main__.py       python -m ninfer_launcher.cli 入口
├── main.py           UTF-8 重配置 + argparse + 子命令分发 + JSON 输出 + 退出码
├── result.py         CliResult 结果对象 + 错误码常量 + JSON 序列化
├── runtime.py        runtime 目录 / PID 文件 / 锁文件 / 日志文件
├── resolve.py        参数来源解析（预设 → 参数值 → exe 路径 → 模型路径）
└── actions.py        status / start / stop / ensure 四个动作的实现
```

放在 `ninfer_launcher/` 下与 `core` / `params` / `ui` 平级。不放在 `core/` 里面——CLI 是
一个**入口层**（和 `ui/` 同级的角色），不是核心能力。

同时会动到 `cli/` 之外的三处：

```
launcher/
├── cli_main.py                    【新增】PyInstaller 打包入口（禁止 import ui / PySide6）
├── ninfer-launcher-cli.spec       【新增】CLI 的 spec（第 9 节）
└── ninfer_launcher/core/
    ├── health_probe.py            【新增】health.py 下移出的纯探测逻辑
    ├── process_control.py         【新增】process.py 下移出的纯进程控制逻辑
    ├── health.py                  【改】只留 HealthPoller，re-export 纯逻辑
    ├── process.py                 【改】只留 ServerProcess，re-export 纯逻辑
    └── config.py                  【改】接收从 ui 迁来的 find_project_root（第 4 节）
```

`core/` 的这几处改动是**为了让纯逻辑可被无 Qt 的调用方复用**，不是给 CLI 开特例：
GUI 的导入路径与行为完全不变，既有测试一个都不用改。

---

## 4. 命令行接口

### 全局参数（所有子命令通用）

| 参数 | 默认 | 说明 |
|---|---|---|
| `--preset <名字>` | 见下 | 用哪个预设的参数 |
| `--exe <路径>` | 见下 | `ninfer-serve.exe` 路径 |
| `--host <主机>` | `127.0.0.1` | 健康检查主机 |
| `--json` | 恒为真 | 保留占位，当前输出恒为 JSON |

### 各子命令

```
ninfer-launcher-cli status [--tail N]
ninfer-launcher-cli start  [--timeout SEC] [--preset NAME] [--exe PATH]
ninfer-launcher-cli stop   [--force]
ninfer-launcher-cli ensure [--timeout SEC] [--preset NAME] [--exe PATH]
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--tail N` | `0` | 附带服务日志尾部 N 行（`status` 专用，上限 200） |
| `--timeout SEC` | `600` | 等待就绪的上限秒数。**注意语义**：见下 |
| `--force` | 关 | 允许停止 `owner != "cli"` 的实例 |

> **`--timeout` 的语义**：这是 CLI 自身的**硬上限**，防止命令永久挂住。调用方（dsh 插件）
> 有自己的一套「1 分钟报告一次、仍在加载就继续等」的节奏，它是通过**杀掉 CLI 子进程**
> 来中止等待的，不依赖这个值。所以这里给一个宽松的默认值（10 分钟），它只是最后的
> 保险绳。

### 参数来源的优先级

**预设**：`--preset` → `settings.json` 的 `last_preset` → `settings.json` 的 `params` 快照
→ 报错 `no-preset`。

**参数值**：预设的 `params` 为底，`model` / `port` 从预设顶层或 `params` 里取（两处都要看，
`config.load_preset` 的兼容格式如此）。缺失的键**走 registry 默认值**
（`values.get(spec.key, spec.default)`）——不在 CLI 里另造一份默认值，`params/registry.py`
是唯一事实源。

**exe 路径**：`--exe` → `settings.json` 的 `exe_path` → 自动探测
「含 `build-ninja/` 的那层目录」下的 `build-ninja/apps/ninfer-serve.exe`。

> GUI 的 `_resolve_exe` 有三级优先级，第一级是「控制面板实时值」。CLI 没有界面，所以第
> 一级换成命令行参数。**不要照抄 GUI 的实现**，那里读的是 widget。

#### 自动探测这一级要先做一次小重构

现成的探测函数 `_find_project_root()`（向上最多 6 级找含 `build-ninja/` 的目录）位于
**`ui/control_panel.py:53`**——**在 ui 层，CLI 不能 import 它**（约束 1）。三个选择：

| 做法 | 评价 |
|---|---|
| **把它移到 `core/config.py`**，`control_panel.py` 改为从那里 import | **推荐**。它本质是路径解析，零 Qt，本来就该在 core |
| CLI 自己写一份 | 不可取。两份实现会漂移，而且漂移了不会报错 |
| CLI 不支持自动探测，`--exe` 或 `settings.exe_path` 必填 | 可接受的降级，但用户第一次用 CLI 时会撞上 `exe-not-found` |

移动时**有个会静默出错的地方**：现有实现用 `Path(__file__).resolve().parent.parent.parent`
从 `ui/control_panel.py` 向上 3 级到 `launcher/`。移到 `core/config.py` 后同样的 3 级会
落到 `launcher/` 的**上一级**——起点偏了一级。因为外层有「向上最多 6 级」的容错，它
**看起来仍然能工作**，只是从错误的起点开始搜，在某些目录布局下会找到另一个含
`build-ninja/` 的目录。

移动时改成用同文件里已有的 `project_root()` 作为起点：它已经正确处理了打包 / 开发两种
模式（`sys.frozen` 判定），不依赖 `__file__` 的相对深度。

> `core/config.py` 已有的 `project_root()` 是**另一个东西**：它返回「程序根目录」
> （打包后是 exe 所在目录，开发时是 `launcher/`），不是「含 build-ninja 的项目根」。
> 两者语义不同，别混用，也别把新函数起个近似的名字。

---

## 5. runtime 目录布局（新增）

```
%LOCALAPPDATA%/ninfer-launcher/          ← 既有配置根，resolve_config_root()
├── settings.json                        ← 既有，CLI 只读
├── presets/                             ← 既有，CLI 只读
├── .deleted_builtin_presets             ← 既有，CLI 不碰
└── runtime/                             ← 【新增】
    ├── serve.pid                        进程登记表（JSON）
    ├── serve.lock                       启动互斥锁
    └── logs/
        ├── serve-20260911-101500.log    服务 stdout/stderr
        └── ...                          保留最近 10 份
```

### `serve.pid` 结构

```json
{
  "schema": 1,
  "pid": 23456,
  "port": 8080,
  "exe": "E:\\ai\\ninfer-4090-native\\build-ninja\\apps\\ninfer-serve.exe",
  "args": ["E:\\ai\\...\\qwen3_8_27b_8-19.ninfer", "--port", "8080", "--kv-dtype", "rk4v4-e8"],
  "model": "E:\\ai\\ninfer-4090\\qwen3_8_27b_8-19.ninfer",
  "preset": "Qwen3.8-27B MTP",
  "owner": "cli",
  "startedAt": 1789456321.5,
  "logPath": "C:\\Users\\...\\runtime\\logs\\serve-20260911-101500.log"
}
```

**原子写入**：先写 `serve.pid.tmp` 再 `os.replace()`。`core/config.py` 的 `save_settings`
已经是这个模式，照抄它，不要用「open 后直接 write」——中途崩溃会留下半个 JSON，之后
每次读都报错。

**陈旧文件处理**：读到 PID 文件后**必须**用 `process_terminated(pid)` 验证。进程已消失
就删掉文件继续走「服务未运行」的分支。绝不能因为「有个文件在」就报 `already-running`。
断电、任务管理器强杀、手动删 exe 都会留下陈旧文件，这是常态而不是异常。

### `serve.lock` 协议

用 `os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)` 创建，成功即持有锁，文件内写入
持有者的 CLI 进程 PID 和时间戳。

- 创建失败（`FileExistsError`）→ 读出里面的 PID → `process_terminated` 判定持有者是否
  还活着 → 已死则视为**陈旧锁**，删除后重试一次（只重试一次，避免两个 CLI 互相抢）。
- 持有者活着 → 说明另一个 `start` / `ensure` 正在拉起服务。此时**不报错**，转入
  「等待那一个的结果」：轮询 `/health` 直到就绪或超时（这正是 `ensure` 想要的行为）。
- 释放锁必须放在 `try/finally` 里。CLI 崩溃时锁会残留，靠上面的陈旧锁判定收拾。

### 日志文件

**这一条不能省**。fire-and-forget 之后没有任何进程接管服务的 stdout/stderr 管道，如果不
重定向到文件，启动失败的原因（显存不够、模型文件损坏、参数非法）会**全部丢失**。GUI 靠
`tail`（deque 保留最近 20 行）在异常退出时给出上下文，CLI 的等价物就是这个日志文件。

- 命名：`serve-<yyyyMMdd-HHmmss>.log`。
- 打开方式：`open(path, 'wb')`，把文件对象直接给 `subprocess.Popen(stdout=..., stderr=...)`
  （两个通道合并进同一个文件，CLI 场景不需要分色）。
- 轮转：`start` 成功后扫描 `logs/`，按文件名排序删除超出 10 份的最旧文件。
- 读取（`status --tail N`）：按字节从尾部读，用 `core/process.py` 的 `decode_output()`
  解码。**不要用 `open(..., 'r', encoding='utf-8')`**——ninfer-serve 的输出可能是 OEM
  代码页，`decode_output` 已经实现了 `utf-8 → OEM → locale → utf-8+replace` 的解码链且
  永不抛异常，直接复用。

---

## 6. 输出契约（唯一事实源）

### JSON 字段

所有子命令输出**同一个形状**。字段缺失时给 `null`，**不要省略键**——调用方就不用写
`if 'pid' in result` 这种防御代码。

| 字段 | 类型 | 说明 |
|---|---|---|
| `ok` | boolean | 动作是否达成目的 |
| `action` | string | `status` / `start` / `stop` / `ensure` |
| `state` | string | `running` / `starting` / `stopped` / `unknown` |
| `health` | string | `ready` / `loading` / `unreachable` |
| `pid` | number \| null | 服务进程 PID |
| `port` | number \| null | 服务端口 |
| `model` | string \| null | 模型文件绝对路径 |
| `preset` | string \| null | 本实例使用的预设名 |
| `owner` | string \| null | `cli` / `gui` / `external` / null |
| `startedAt` | number \| null | 实例启动时刻（Unix 秒，浮点） |
| `elapsedMs` | number | 本次命令耗时 |
| `waitedMs` | number \| null | 本次为等待就绪花的时间 |
| `vramFreedBytes` | number \| null | `stop` 释放的显存字节数 |
| `settle` | string \| null | `settled` / `timeout` / `degraded`（显存回落结果） |
| `logPath` | string \| null | 服务日志文件绝对路径 |
| `logTail` | string[] \| null | 日志尾部行（仅 `--tail N > 0` 时非 null） |
| `message` | string | 面向人的中文说明，可直接展示给用户 |
| `error` | string \| null | 失败时的稳定错误码，成功时 null |

### 错误码

`error` 用**稳定标识符**，不用中文文案。调用方靠它分支，中文放 `message`——文案改了不
应该让调用方的逻辑挂掉。

| 错误码 | 含义 | 调用方应有的反应 |
|---|---|---|
| `no-preset` | 找不到可用的预设 | 配置问题，报给用户，不重试 |
| `invalid-params` | `validate_values` 未通过 | 配置问题，报给用户，`message` 含逐条原因 |
| `model-not-found` | 模型文件不存在 | 配置问题，报给用户 |
| `exe-not-found` | `ninfer-serve.exe` 不存在 | 配置问题，报给用户 |
| `port-in-use` | 端口被别的进程占着（且不是我们的服务） | 报给用户，可能是残留进程 |
| `port-mismatch` | start/ensure：已登记的存活实例在**另一个端口**（docs 12.3） | 报给用户：先 stop 再启动 |
| `spawn-failed` | `Popen` 失败 | 报给用户，`message` 含系统错误 |
| `health-timeout` | 等到超时仍未就绪 | 看 `state`：`starting` 说明还在加载，可继续等 |
| `crashed` | 等待期间进程消失了 | 真失败，`logTail` 里有原因 |
| `not-running` | `stop` 时服务本来就没在跑 | 视为成功语义（幂等），无需处理 |
| `no-pid-to-stop` | `stop --force`：外部实例活着但没有可定位的 PID（docs 12.5） | **必须提示用户手动结束进程** |
| `kill-timeout` | 强杀升级 15 秒用尽仍未死 | **必须告知用户手动结束进程** |
| `not-owned` | `stop` 目标不是 `owner: "cli"` 且未加 `--force` | 按设计跳过，不是错误 |
| `lock-timeout` | 等待另一个实例拉起时超时 | 同 `health-timeout` |

### 退出码

| 码 | 含义 |
|---|---|
| `0` | `ok: true` |
| `1` | `ok: false`（业务失败） |
| `2` | 用法错误（argparse 层面） |

调用方**优先读 `error` 字段**，退出码只做兜底（比如 CLI 自己崩了没输出 JSON 的情况）。

---

## 7. 四个动作的详细流程

### 7.1 `status`

无副作用，不加锁。

```
1. root = resolve_config_root()
2. entry = 读 runtime/serve.pid（不存在则 None）
3. port = entry.port（有）→ --preset 的预设 port → settings.params 的 port → 8080
4. health = probe_health(host, port) → classify_health()
5. alive = entry 存在 and not process_terminated(entry.pid)
6. 按第 2 节的组合表判定 state 与 owner：
   - health ready               → running
   - health loading and alive   → starting
   - health unreachable, alive  → starting
   - health unreachable, 不活   → stopped
   - health ready, entry 为 None → running + owner="external"
7. entry 存在但进程已死 → 删除陈旧 PID 文件（这是 status 唯一的副作用，可接受：
   它消除的是一个已经确定为假的记录）
8. --tail N > 0 且有 logPath → 读尾部 N 行（decode_output 解码）
9. 输出 JSON，ok = true（status 只要跑完就算成功，不论服务在不在）
```

### 7.2 `start`

```
1. root = resolve_config_root()
2. 取锁（第 5 节协议）。取不到且持有者活着 → 走 ensure 的「等待他人结果」分支
3. 前置校验（任一失败立即返回，不 spawn）：
   a. 解析预设与参数值（第 4 节优先级）→ 失败 no-preset
   b. validate_values(values)          → 失败 invalid-params，message 含逐条原因
   c. model 非空 and os.path.isfile()  → 失败 model-not-found
      （提前拦截，否则 CreateFileW 找不到文件的报错很难懂）
   d. 解析 exe and os.path.isfile()    → 失败 exe-not-found
   e. probe_health → 已经 ready？→ 直接返回 ok:true, state:running（幂等）
   e2. 进程登记表存在**存活实例**（load_pid_entry 已做 OS 级存活校验）：
       - 端口一致（或登记未记端口）→ 【不重复 spawn】等它就绪——前任 CLI 还在
         加载，接着等即可（start / ensure 共用此分支，见 12.7）；
       - 端口不一致 → 【失败 port-mismatch】：旧端口的实例还活着，再拉一个会把
         两份权重压上同一张卡；报给用户先 stop 再启动，绝不擅自重启（docs 12.3）
   f. check_port(port) 占用且 health 不 ready → 失败 port-in-use
      （顺序很重要：先查 health 再查端口。端口被我们自己的服务占着是正常情况，
        只有「端口被占但那上面不是我们的服务」才是错误）
4. args = build(values)（联动禁用的参数自动跳过）
5. 开日志文件 logs/serve-<时间戳>.log（'wb'）
6. detached spawn：
   subprocess.Popen(
       [exe, *args],
       cwd=os.path.dirname(exe),          # 和 GUI 一致
       stdout=log_file, stderr=log_file,  # 【不能省】
       stdin=subprocess.DEVNULL,
       creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
       close_fds=True,
   )
   # CREATE_NEW_PROCESS_GROUP 是为了将来能发 CTRL_BREAK_EVENT，
   # 也避免 CLI 被 Ctrl+C 时信号传到服务进程
7. 写 runtime/serve.pid（owner="cli"，原子写入）
8. 轮询就绪，间隔 500 ms（沿用 health.HEALTH_INTERVAL_MS 的值）：
   每轮先 process_terminated(pid) —— 进程死了立刻返回 crashed + logTail，
   不要傻等到 timeout；然后 probe_health。
   ready  → 返回 ok:true, state:running, waitedMs
   超时   → 返回 ok:false, error:health-timeout，
            state 按当前 health 给 starting，附 logTail 便于定位
9. finally: 释放锁、关闭日志文件对象、轮转旧日志
```

> **为什么第 8 步每轮都要查进程**：GUI 里这件事由 `HealthPoller` 的 `abort_check`
> 回调完成（传的是 `process.abort_reason`），注释写得很清楚：避免「服务早退还在傻等
> 健康检查」。CLI 没有 `ServerProcess`，必须自己做等价的检查。

### 7.3 `stop`

复用现成纯函数，顺序照搬 GUI 验证过的链条，但**跳过阶段 0**。

```
1. entry = 读 runtime/serve.pid
   不存在 → 若 /health 有响应则有个外部实例活着（owner=external）：
            未加 --force → ok:true, error:not-owned, message 说明「非本工具启动，跳过」
            加了 --force → ok:false, error:no-pid-to-stop——我们不按端口反查 PID，
                        停不掉它；message 必须写明「进程仍在运行、未停止，请手动
                        结束该进程」。这里报 ok:true 会让调用方误以为已停（docs 12.5）
   不存在且 health 不通 → ok:true, error:not-running（幂等语义，不算失败）
2. entry.owner != "cli" 且未加 --force → ok:true, error:not-owned，不做任何事
3. process_terminated(entry.pid) 已死 → 删 PID 文件，ok:true, error:not-running
4. 记显存基线：monitor = MonitorService(); reader = make_vram_reader(monitor)
5. 【阶段 1】run_taskkill(pid)          # taskkill /PID <pid> /T /F，杀整个进程树
6. process_terminated 校验；仍活 →
   【阶段 2】循环：每 1 秒 terminate_process_hard(pid) + process_terminated 校验，
             上限 15 秒（对齐 KILL_ESCALATION_SECONDS / KILL_CHECK_INTERVAL_MS）
7. 15 秒用尽仍活 → ok:false, error:kill-timeout，
   message 必须明确写「请在任务管理器中手动结束 ninfer-serve.exe」
   —— 照搬 GUI 的原则：宁可带警告退出，也不静默留下占着 23 GB 显存的残留进程
8. 进程确认死亡 → settle = settle_vram(reader=reader)
   结果映射到 JSON 的 settle / vramFreedBytes 字段
9. monitor.shutdown()（释放 NVML）
10. 删除 runtime/serve.pid
11. 输出 JSON
```

> **为什么跳过阶段 0**：GUI 的阶段 0 是 `QProcess.terminate()`（软终止），需要 QProcess
> 句柄。CLI 停的是别的进程启的服务，没有句柄。所以 CLI 的停止比 GUI 更「硬」——直接从
> taskkill 开始。ninfer-serve 没有需要优雅退出的持久化状态，这个差异可接受。
>
> 若要对齐 GUI 的柔和度，可以在阶段 1 之前先
> `os.kill(pid, signal.CTRL_BREAK_EVENT)` 并给一个短宽限期——因为 spawn 时用了
> `CREATE_NEW_PROCESS_GROUP`，这条路是通的。**属于可选增强，不是必需项。**

### 7.4 `ensure`

插件唯一调用的入口。语义是「保证服务可用」，不是「启动服务」。

```
1. probe_health → ready？→ 立即返回 ok:true, state:running, waitedMs:0
   （这是绝大多数调用走的路径，必须快：一次 HTTP 请求就返回，不读 PID 文件、不取锁；
    owner 返回 null——快速路径没读登记表、无法断言归属，「未知」就是 null（docs 12.2））
2. 尝试取锁：
   取到       → 走 start 的完整流程（第 7.2 节第 3 步起）
   取不到且持有者活着 → 「等待他人结果」分支：
                        轮询 /health 直到 ready 或 --timeout 用尽
                        超时返回 error:lock-timeout
   取不到但持有者已死  → 删陈旧锁，重试取锁一次
3. 进程活着但 health 是 loading → 【不重复拉起】，直接进入轮询等待
   这是 ensure 与 start 的关键区别：start 看到「已在跑」会直接返回，
   ensure 要等到真正 ready 才返回
```

---

## 8. 并发与竞态

三种并发情形，处理方式都在上面，这里汇总成表便于自查：

| 情形 | 处理 |
|---|---|
| 两个 `ensure` 同时到 | 锁互斥。抢到的去拉起，没抢到的轮询等结果。**都返回成功**，不报冲突 |
| `ensure` 与 GUI 的「启动」同时点 | GUI 不参与锁协议。靠 `check_port` + 先查 `/health` 拦住：谁先起来另一个就复用它 |
| `stop` 与 `ensure` 同时 | 不加互斥（`stop` 不取锁）。竞态窗口存在，由**调用方**用「请求失败后重新 ensure」兜底（见插件文档第 6.4 节） |

> `stop` 为什么不取锁：`stop` 常常发生在「要赶紧释放显存」的场合，让它去等一个可能长达
> 一分钟的启动锁是错的。竞态的代价只是一次请求失败后重启，插件侧已有兜底。

---

## 9. 打包与构建

### 新增 spec

新建 `ninfer-launcher-cli.spec`，参照现有 `ninfer-launcher.spec` 但有三处关键差异：

| 项 | GUI spec | CLI spec |
|---|---|---|
| `console` | `False` | **`True`** |
| `datas` | `resources/` + `qtbase_zh_CN.qm` | **不需要**（CLI 不读预设资源？见下） |
| `excludes` | tkinter/matplotlib/numpy/... | 加上 **`PySide6`** |

- **`console=True` 是硬要求**。GUI 的 exe 是 `console=False`，**它没有控制台，stdout 无处
  可去**——调用方读不到那行 JSON。这就是为什么 CLI 必须是独立 exe 而不是给 GUI exe 加参数。
- `excludes` 加 `PySide6`：如果代码里不小心 import 了 `ui`，PyInstaller 会因为找不到被
  排除的模块而**在构建期报错**。这把「约束 1」从一条纪律变成一道自动关卡，值得加。
- `datas`：CLI 只读**配置根**里的预设（`%LOCALAPPDATA%`），不需要随包分发
  `resources/presets/`——播种是 GUI 启动时做的事。若希望 CLI 在配置根为空时也能工作，
  再把 `resources/presets/` 加进来并调用 `seed_builtin_presets()`；**建议不加**，让
  「配置根没有预设」明确报 `no-preset`，而不是悄悄用出厂预设起一个用户没配过的服务。

### `build.ps1` 改动

在现有七步流程里插入，不要新建一个并行脚本：

- 第 4 步之后加一步：按 `ninfer-launcher-cli.spec` 构建 CLI。
- 第 6 步（校验产物）扩展：同时校验 CLI exe 存在并打印体积。
- **新增一项冒烟校验**：跑 `dist\ninfer-launcher-cli\ninfer-launcher-cli.exe status`，
  断言退出码为 0 且 stdout 能被 `json.loads` 解析。这是唯一能证明「输出契约没被打破」的
  自动检查，比检查 exe 存在有意义得多。

---

## 10. 测试策略

沿用仓库现有的测试纪律（`tests/conftest.py` 的两条 autouse 夹具、`realsystem` 标记默认
排除）。CLI 的测试**不需要** QApplication，但会话级夹具已经建好了，无害。

### 必须覆盖的用例

| 用例 | 要点 |
|---|---|
| 输出契约 | 每个子命令的输出都能 `json.loads`，且**键集合完整**（缺键就是破坏契约） |
| 状态判定组合 | 第 2 节那张表逐行验证，注入假 `probe_health` 和假 `process_terminated` |
| 陈旧 PID 文件 | PID 文件存在但进程已死 → 判 `stopped` 且文件被删，**不能报 already-running** |
| 陈旧锁 | 锁文件持有者已死 → 夺锁成功 |
| 锁互斥 | 持有者活着 → 走等待分支而不是报错 |
| 启动前校验 | 五项校验各自的失败都返回对应错误码，且**没有 spawn 发生** |
| 等待期进程死亡 | 轮询中进程消失 → 立刻返回 `crashed` + `logTail`，不等到超时 |
| stop 归属 | `owner != "cli"` 且无 `--force` → 不执行任何 kill |
| stop 强杀升级 | 注入「永远杀不死」的假 killer → 返回 `kill-timeout` 且 message 含手动提示 |
| stop 幂等 | 服务本来就没跑 → `ok: true` + `not-running` |
| 日志尾部解码 | 写一段 GBK 字节进日志文件，`--tail` 能读出可读文本不抛异常 |

### 禁止事项

- **不许真的 spawn `ninfer-serve.exe`**。`Popen` 必须可注入替身。需要真进程的场景用
  `ping.exe`（`tests/test_close_kill.py` 已有这个先例，那里用真进程做 OS 真值闭环）。
- **不许碰真实配置根**。`monkeypatch` `resolve_config_root` 指到 `tmp_path`，这是仓库
  既有纪律（`test_show_command.py` 等 UI 测试都这么做）。
- **不许碰真实 GPU 驱动**。`conftest` 的第二条 autouse 夹具已经拦掉了 `gpu_power`；
  `MonitorService` 在 CLI 测试里要注入假 reader。

---

## 11. 建议的实现顺序

分四个阶段，每个阶段结束都可独立验证。

**阶段 1：骨架与契约**
`result.py`（结果对象 + 错误码）→ `main.py`（UTF-8 + argparse + 分发）→ `status` 动作。
验证：`python -m ninfer_launcher.cli status` 输出合法 JSON。这一步就能把输出契约钉死。

**阶段 2：runtime 层**
`runtime.py`（PID 文件原子读写、锁协议、日志目录与轮转）。
验证：单测覆盖陈旧 PID / 陈旧锁 / 锁互斥三种情形。

**阶段 3：start / ensure**
`resolve.py`（参数来源）→ `actions.start` → `actions.ensure`。
验证：注入假 `Popen` 和假 `probe_health` 跑完整流程；然后**手工跑一次真实启动**，
记录实际冷启动耗时（这个数字插件侧要用，见插件文档第 9 节）。

**阶段 4：stop 与打包**
`actions.stop`（三级链 + 显存回落）→ CLI spec → `build.ps1` 改动 + 冒烟校验。
验证：手工跑一次真实的 `start` → `stop` 全程，确认进程真死、显存真降。

---

## 12. 实现后复盘：缺陷与优化项

本节记录 2026-09-11 首版实现完成后的复盘结论。基线：`tests` 全量 **454 passed,
3 deselected**（realsystem 默认排除），`python -m ninfer_launcher.cli status --tail 5`
实跑输出合法单行 JSON、中文正常、外部实例被正确判为 `owner: "external"`。

原复盘的「第 1 项是必须修的缺陷，其余四项是优化建议，按严重度排列」——**五项已于
2026-09-12 全部落地**，每一项都有对应回归测试（基线随之变为 **462 passed,
3 deselected**）。下面保留复盘原文以便追溯根因，每项末尾追加修复结论与测试入口；
唯一仍开放的是 12.6 的未验证清单（均为构建 / 实机测量项，与代码修复无关）。

### 12.1 【已修复】`find_project_root` 循环不递进

`core/config.py` 迁移后的实现里，循环体**丢了 `p = p.parent`**：

```python
    base = project_root()
    p = base
    for _ in range(6):
        if p.is_dir() and (p / "build-ninja").is_dir():
            return p
        # ← 缺 p = p.parent，6 次循环全在检查同一个目录
    return base.parent
```

6 次循环等价于只检查了 `project_root()` 本身，然后总是落到回退分支。

**实测证据**（monkeypatch `project_root` 模拟打包模式）：

```
dev mode  : E:\ai\ninfer-4090-native                 ← 碰巧正确
frozen sim: E:\ai\ninfer-4090-native\launcher\dist   ← 错误，应为 E:\ai\ninfer-4090-native
```

开发模式下之所以正确，是因为回退值 `base.parent`（`launcher/` 的上级）**恰好就是**项目根。
这正是第 4 节警告过的「看起来仍然正常」，只是原因从「层级偏移」变成了「循环不递进」。

**影响面比 CLI 更大**：`ui/control_panel._auto_detect` 与 `ui/main_window._resolve_exe`
也调这个函数，所以**GUI 打包版的 exe 自动探测同时坏了**——这是迁移引入的 regression。
开发模式下两边都碰巧正确，**454 个测试一个都不会红**。

**已修复**（2026-09-12）：`core/config.py` 补回丢失的 `p = p.parent`（函数 docstring
同步写明本 bug 与回归测试入口）。上面建议的回归测试落在
`tests/test_config.py::TestFindProjectRoot`：monkeypatch `project_root` 返回**比假项目根
深两级**的起点（等价 onedir 打包布局），断言能向上走到含 `build-ninja/` 的层——旧实现在
此输入下返回起点的上一级，断言失败；另有起点即项目根、找不到 build-ninja 走回退两条
用例。GUI 的 `control_panel._auto_detect` / `main_window._resolve_exe` 共用同一函数，
打包版的 exe 自动探测随本次修复一并恢复。

### 12.2 【已修复】`ensure` 快速路径的 `owner` 不该断言 `external`

`action_ensure` 的快速路径只探一次 `/health` 就返回（这是对的，热路径必须快），但它
返回了 `owner: "external"`。**它没读 PID 文件，并不知道归属**——服务明明是本工具启的，
也会被报成 external。

契约允许 `owner` 为 `null`，「未知」就该用 `null`；`external` 是一个明确的断言
（「不是本工具启的」），不该在没查证的情况下给出。

插件侧不依赖这个字段（它有自己的 `startedByUs` 记录），但 `/localmodel status` 会把它
显示给用户，会误导排查。改为 `None` 即可，热路径开销不变。

**已修复**（2026-09-12）：`action_ensure` 快速路径现在返回 `owner=None`（第 6 节契约
本就允许 `null` = 「未知」；`external` 是明确断言，不查证不给）。热路径仍然只探一次
`/health` 即返回，开销不变。回归：`test_ensure_fast_path_ready_no_lock` 追加了
`owner is None` 断言防回退。

### 12.3 【已修复】换端口的旧实例可能导致两个实例并存

`_start_flow` 复用已有实例的条件是 `entry.port is None or entry.port == port`。端口不同
时不复用，继续走端口检查并 spawn——于是**旧实例还在 8080 跑着，新实例在 8081 起来，
两份 23 GB 权重挤同一张卡**。

设计阶段的决策「服务已在跑就直接复用、不比对参数」只覆盖了同端口场景，**换端口是个没
讨论到的缺口**。这不是判断失误，是范围遗漏。

建议：spawn 之前检查是否存在**任何** `owner: "cli"` 的存活实例（不论端口）。存在则二选一
（推荐前者）：

- 报一个新错误码（如 `port-mismatch`），message 写明「已有实例在端口 X，当前配置要求
  端口 Y，请先 stop」——让用户显式决定；
- 先 stop 旧实例后启新的——省事但会让用户白等一次冷启动，且违背「不擅自重启」的既定决策。

**已修复**（2026-09-12），采用**前者**（报错误码、不擅自重启）：`_start_flow` 在步骤 e
（已 ready 即返回）之后、端口检查之前新增一步——登记表里存在**端口与本次请求不同**的
存活实例时，返回新错误码 `port-mismatch`（第 6 节错误码表与第 7.2 节流程已同步），
message 写明「已有实例在端口 X、配置要求端口 Y，请先 stop 再启动」。拦截范围比建议更宽
一档：**任何归属**的存活登记实例（不只 `owner: "cli"`）——归属不明的实例（如登记表
损坏回落 `external`）同样不该被静默地再拉一份权重。回归测试：
`test_start_port_mismatch_blocks_second_instance`（cli 实例在 18081、请求 18080 →
`port-mismatch` 且 `spawned() == 0`、登记表不被破坏）与
`test_start_port_mismatch_blocks_external_owner_instance`（external 归属同样拦截）。

### 12.4 【已修复】`read_log_tail` 应从文件尾部读，而不是读全量

当前实现是 `fh.read()` 读整个文件再 `splitlines()` 取尾部。服务长时间运行日志可能到几十
MB，而一次 `status --tail 20` 只需要最后几 KB。

建议 `seek` 到末尾往前读固定块（64 KB 足够 20 行），不够行数再往前扩一块。注意两点：
**仍然必须走 `decode_output` 解码**（陷阱 9），以及**按块读会切断首行的多字节字符**——
解码后丢弃第一个不完整行即可（反正只要尾部）。

**已修复**（2026-09-12）：`runtime.read_log_tail` 现在默认从文件**尾部**读 64 KiB 一块，
行数不够就把块**加倍**向前扩，直到凑够或读到文件头（加倍使上百 MB 的文件十几轮也到
头；正常路径只读一块）。实测 56.5 MB 日志取 20 行约 10 ms，结果与整读完全一致。两个
实现细节值得记下：

1. 解码仍走 `decode_output`（陷阱 9），没有换成固定编码直读；
2. 块起点落在行中间时，按「前进到第一个换行、丢弃不完整首行」处理，**不是**按字节硬切
   后直接解码——块起点切断一个多字节字符会让整块 UTF-8 解码失败、`decode_output` 落到
   OEM 回退，**整段尾部**都会乱码而不只是首行；换行符不可能出现在多字节字符内部
   （UTF-8 / GBK 的后续字节都不含 `0x0A`），按行边界对齐就不切字符，剩余块仍可整体
   用单一编码解码。

回归测试：`test_read_log_tail_large_file_reads_from_tail`（300 KB 日志，结果与整读
一致）、`test_read_log_tail_grows_block_until_enough_lines`（单行 40 KB，逼出块扩
展路径）、`test_read_log_tail_large_file_gbk_decodes_via_chain`（120 KB GBK 日志，
验证行边界对齐的块能走探测链解码）。

### 12.5 【已修复】`stop` 外部实例 + `--force` 的 `ok` 语义

外部实例存在（有 `/health` 响应但没有 PID 文件）且带 `--force` 时，当前返回
`ok: true` + `error: "not-running"`，message 写「请手动结束该进程」——**实际什么都没停，
却报了成功**。调用方按 `ok` 分支会误判成已停止。

建议改 `ok: false`。`not-running` 在契约里的「视为成功语义」指的是**服务本来就不在跑**
（幂等），而这里服务明明在跑、只是我们停不了它，语义不同。可以另给一个错误码
（如 `no-pid-to-stop`）把两种情况分开。

**已修复**（2026-09-12）：该分支现返回 `ok: false` + 新错误码 `no-pid-to-stop`
（第 6 节错误码表与第 7.3 节流程已同步），message 保留「请手动结束该进程」的指引。
两种语义就此分开：`not-running` = 「服务本来就没在跑」（`ok: true`，幂等成功）；
`no-pid-to-stop` = 「在跑但我们停不掉」（`ok: false`，失败）。回归：
`test_stop_external_instance_force_reports_no_pid` 断言 `ok is False` +
`error == "no-pid-to-stop"` + message 含手动处理指引。

### 12.6 尚未验证的部分

| 项 | 状态 | 说明 |
|---|---|---|
| 打包产物 | **未验证** | `dist/` 下只有 `ninfer-launcher`，没有 `ninfer-launcher-cli`。spec、`build.ps1` 第 5 步、产物校验、status 冒烟全部还是纸面的 |
| `console=True` | **未验证** | 只有构建后才能确认 stdout 真的能被调用方读到——这是 CLI 必须独立 exe 的全部理由 |
| `excludes PySide6` 关卡 | **未验证** | 同上，要构建才会触发 |
| 真实 `start` / `stop` 全程 | **未验证** | 复盘时服务正由 GUI 运行中，未去动它 |
| CLI exe 冷启动耗时 | **未实测** | 插件侧「热路径用 fetch、只在探测失败才 spawn CLI」这个优化的依据 |
| ninfer-serve 加载到 `/health` 200 的耗时 | **未实测** | 插件侧提示文案与 `waitReportSeconds` 的取值依据 |

后两项在 `02-dsh-plugin-auto-lifecycle.md` 第 9.3 节列为待验证点，建议构建完成后一并测掉
并把数字回填到那份文档里。

> **状态（2026-09-12）**：12.1–12.5 五个代码项已全部修复且测试全绿（462 passed），
> 但**本节各项状态未变**——它们都属于「构建 / 实机测量」项：打包流程（spec 与
> `build.ps1` 第 5 步）尚未实跑，真实 start / stop 循环与两个耗时数字也未实测。
> 首次打包完成后应逐项勾掉并把结果（含耗时数字）回填到插件文档。

### 12.7 值得保留的实现改进（不要在后续重构中丢掉）

复盘发现实现比本文档原稿更好的几处，已回写进前面各节，这里汇总备查：

- **`test_cli_package_is_qt_free`**：断言 CLI 包不拖 Qt，把约束 1 从纪律变成每次跑测试
  就检查的关卡（原稿只靠 spec 的 `excludes` 在构建期兜）。
- **纯逻辑下移 + 原模块 re-export**（第 2 节已更正）：解决了「函数不依赖 Qt 但模块顶层
  import Qt」这个原稿没意识到的问题。
- **`_start_flow` 的「已有实例正在加载则等待、不重复 spawn」**：原稿只在 `ensure` 里提，
  实现放进 start/ensure 共用路径，两者都受益。
- **`stop` 之后复探 `/health`**：强杀超时时如实报 `health: ready` + `state: unknown`，
  不假装进程已经没了。
- **`_wait_for_other` 用回调取 PID / 日志路径**：处理了「等别人启动时 PID 文件中途才
  出现」的情况，固定值在这里不够用。
- **`kill-timeout` 时保留 PID 文件**：进程还活着，登记表就不能删——否则下次 `stop` 找不到
  它，残留进程再也无法通过本工具处理。

---

## 13. 陷阱清单（改了不会报错的那些）

按「做错了验证不会变红」筛选，逐条都是静默失效：

1. **忘了 `sys.stdout.reconfigure(encoding='utf-8')`**：中文 `message` 在 GBK 控制台变
   乱码，调用方 `JSON.parse` 挂掉，但 CLI 自己退出码是 0。
2. **`cli/` 里 import 了 `ui`**：PySide6 被拖进来，启动慢几百毫秒，功能完全正常。
   靠 spec 的 `excludes PySide6` 把它变成构建期错误。
3. **PID 文件当状态真值**：断电后残留文件让 `ensure` 以为服务在跑，直接返回成功，然后
   插件的请求全部连接失败。必须 `process_terminated` 验证。
4. **没重定向服务的 stdout/stderr**：启动失败时 `logTail` 是空的，你永远查不出显存不够
   还是模型损坏。这个错误只在出问题那天暴露。
5. **`start` 里先查端口后查 health**：端口被自己的服务占着会被误判成 `port-in-use`，
   于是明明服务好着却拒绝工作。顺序必须是先 health 后端口。
6. **轮询就绪时不查进程存活**：服务起来就崩了，CLI 会傻等满 10 分钟才报 timeout，
   而真实原因（崩溃）被超时掩盖。
7. **JSON 里省略 null 键**：调用方写了 `result.pid` 拿到 `undefined`，和「pid 是 0」
   无法区分。键必须齐全。
8. **写了 `settings.json`**：CLI 悄悄改掉 `last_preset`，用户下次开 GUI 发现预设变了，
   完全无法归因。
9. **`decode_output` 换成 `open(encoding='utf-8')`**：ninfer-serve 输出 OEM 代码页时
   直接抛 `UnicodeDecodeError`，把 `status --tail` 整个搞挂。
10. **改动 `core/process.py` 的停止链纯函数**：那三个函数 GUI 也在用，改了会同时影响
    GUI 的停止行为。CLI 只调用它们，**不修改**。要加行为就在 `cli/actions.py` 里组合。
11. **锁没放在 `try/finally` 里**：CLI 异常退出留下锁，下一次 `ensure` 走等待分支等到
    超时——表现是「启动特别慢」而不是「有个锁没释放」。
12. **原子写入退化成直接 write**：写 PID 文件途中崩溃留下半个 JSON，之后每次读都抛
    异常。照抄 `config.save_settings` 的 `.tmp` + `os.replace`。
13. **搬 `_find_project_root` 时照抄 `__file__` 的层级数**：从 `ui/` 搬到 `core/` 后
    向上 3 级会偏一级，而「向上最多 6 级」的容错让它看起来仍然正常。改用
    `project_root()` 作为起点（见第 4 节）。搬完要跑一次 GUI 确认它的 exe 自动探测
    没坏——那条路径是 `control_panel._auto_detect` 在用的。
14. **从「顶层 import Qt 的模块」里导入纯函数**：`from core.health import probe_health`
    看着没问题——函数本身零 Qt——但 `core/health.py` 顶层就 `import PySide6`，导入它等于
    加载整个 Qt。判断依据是**模块的顶层 import**，不是函数体（见第 2 节）。
15. **循环式向上搜索忘了递进**（本项目已实际发生，见 12.1）：`for _ in range(6)` 里漏掉
    `p = p.parent`，循环退化成重复检查同一个目录，然后靠回退分支返回一个**在开发模式下
    恰好正确**的值。判断这类函数是否正确，不能只看开发模式下的输出对不对，要注入一个
    不同深度的起点验证它真的在往上走。

---

## 14. 使用方式（已实现）

### 14.1 开发环境直接运行（免安装）

在 `launcher/` 目录下用仓库自带 Python（3.10+，与 PySide6 同解释器）：

```powershell
cd E:\ai\ninfer-4090-native\launcher
python -m ninfer_launcher.cli status
python -m ninfer_launcher.cli start  --preset P1 --timeout 900
python -m ninfer_launcher.cli stop
python -m ninfer_launcher.cli stop --force     # 强停非本 CLI 拉起的实例
python -m ninfer_launcher.cli ensure --preset P1   # 幂等，插件唯一入口
```

所有选项：`status [--tail N]`（日志尾行数，上限 200，**默认 0 即不附带日志**——需要日志
必须显式传，`--tail 20` 是插件诊断路径的惯例值）；`start` / `ensure` 共用
`[--preset 名] [--exe 路径] [--host 地址] [--timeout 秒]`（timeout 仅硬上限，默认 600，
插件靠杀 CLI 子进程提前中止等待）；`stop` 另带 `[--force]`。公共选项 `--preset` / `--exe`
/ `--host` 可加在任意动作上。

### 14.2 发布版 exe

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build.ps1
```

脚本第 5 步构建 CLI（`ninfer-launcher-cli.spec`，`excludes PySide6/shiboken6`），第 7 步
校验产物并自动跑 `status` 冒烟验证 JSON 可解析。预期产物：

- `dist\ninfer-launcher-cli\ninfer-launcher-cli.exe` —— 控制台程序（无 GUI 依赖），
  供 dsh 插件以子进程方式 spawn；
- `dist\ninfer-launcher\ninfer-launcher.exe` —— GUI，行为不变。

> **状态：构建流程已写好但尚未实跑过**（`dist/` 下目前只有 GUI 产物）。`console=True`
> 能否让调用方读到 stdout、`excludes PySide6` 关卡是否真的拦得住、CLI exe 的实际冷启动
> 耗时，三项都要等第一次构建后才能确认。详见 12.6。

### 14.3 输出与退出码

stdout 永远**恰好一行 UTF-8 JSON**（18 键齐全，第 6 节契约）；人类可读的进度信息走 stderr。
退出码：`0` = 成功（含 `not-running` / `not-owned` 等幂等跳过），`1` = 业务失败或未预期
异常，`2` = 命令行用法错误。

真实输出示例（本机已有外部实例在 8080 端口服务时执行 `status`）：

```json
{"ok":true,"action":"status","state":"running","health":"ready","pid":null,"port":8080,"model":null,"preset":null,"owner":"external","startedAt":null,"elapsedMs":130.9,"waitedMs":null,"vramFreedBytes":null,"settle":null,"logPath":null,"logTail":null,"message":"服务运行中（端口 8080，非本工具启动的实例）","error":null}
```

### 14.4 与 GUI 的分工

- 配置、参数编辑、预设管理 → 仍然只在 GUI（CLI 只读，从不写 `settings.json`）；
- 无人值守的拉起/停止/状态查询 → CLI（dsh 插件 spawn `ensure` 即可）；
- 同一时刻只应有一个 CLI 实例在 `start`/`ensure`（`serve.lock` 互斥；另一个 CLI 会等
  第一个完成后接管结果，而不是重复拉起）。
