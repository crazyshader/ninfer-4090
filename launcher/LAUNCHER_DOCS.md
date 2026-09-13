# NInfer Launcher 使用与实现说明文档

> 适用对象：ninfer-launcher（launcher/ 目录）—— ninfer-serve 的图形化启动器。
> 目标平台：Windows 11 + RTX 4090（sm_89），Python 3.10+（本机 3.14），PySide6 / nvidia-ml-py / psutil。
> 本文档覆盖：项目结构、各模块实现原理、完整运行流程、配置与预设体系、构建打包、测试策略、常见问题排查。

---

## 1. 项目定位与总体架构

ninfer-launcher 是一个 PySide6 (Qt) 桌面 GUI，负责把 ninfer-serve.exe（C++ 推理服务器）当子进程拉起并全程管理其生命周期：

- 启动前：校验参数 / 模型文件 / 端口占用 → 组装命令行 → QProcess 拉起服务；
- 启动中：每 500 ms 轮询 /health 端点判定就绪（200=READY / 503=加载中）；
- 运行中：NVML/nvidia-smi 周期采集 GPU/CPU/内存读数展示在监视面板；
- 停止时：terminate → taskkill 杀进程树 → Win32 TerminateProcess 强杀升级（CUDA 进程在 GPU 驱动临界区会拒绝终止，这是 Windows 上实测确认的行为），全程以 OS 层 OpenProcess 探测作为「进程已消失」的唯一真值；
- 收尾：等待显存回落（或降级固定等待），并把 NVIDIA 驱动的「电源管理模式」写回出厂默认。

### 1.1 分层结构与依赖方向

```
main.py                      入口：QApplication + 装 Qt 内置简体中文翻译 + MainWindow
└── ninfer_launcher/
    ├── core/                纯业务逻辑层（可脱离 UI 单测）
    │   ├── config.py        配置根解析、settings.json、预设 CRUD、内置预设播种/墓碑
    │   ├── ports.py         端口占用检测（SO_EXCLUSIVEADDRUSE）与顺延建议
    │   ├── health.py        /health 周期探测（HealthPoller）
    │   ├── process.py       ServerProcess：四态状态机 + QProcess + 停止升级链 + 显存回落
    │   ├── monitor.py       MonitorService：NVML → nvidia-smi → 不可用 三级来源
    │   └── gpu_power.py     NVAPI DRS 读写「电源管理模式」（ctypes，Win-only）
    ├── params/              参数元数据层（registry 是唯一事实源）
    │   ├── spec.py          ParamSpec / ParamKind / Bool3 / EmitPolicy
    │   ├── registry.py      12 个参数的完整定义（key/flag/默认值/分组/联动）
    │   └── builder.py       值字典 → 命令行 list[str]（纯函数）
    └── ui/                  PySide6 界面层
        ├── main_window.py   ★ 唯一持有 ServerProcess/HealthPoller/ConfigStore 的编排中枢
        ├── control_panel.py 控制面板（通用设置/预设组/服务器控制/显存预检/资源监视/响应式双列）
        ├── params_tab.py    参数面板（registry 驱动生成控件 + 搜索 + 高亮 + 联动）
        ├── widgets.py       通用控件库（Bool3CheckBox / NullableSpinBox / Highlighted* / FlagLabel）
        ├── gpu_power_switch.py 「性能模式」开关控件
        ├── monitor_panel.py 资源监视面板（只消费 MonitorService，不自己采集）
        ├── log_panel.py     日志面板（三通道着色 + 导出）
        └── theme.py         深色/浅色/跟随系统 主题（调色板 + 整份 QSS 即时切换）
```

关键设计原则（代码注释里反复强调的契约）：

1. core/params 层零 Qt 依赖（gpu_power 除外，它用 ctypes）：纯逻辑与「碰真实系统」严格分三段，全部通过构造参数注入替身，测试不需要 GPU、不需要真实驱动、不需要装 pynvml。
2. ui 层不知道进程管理与配置存取的实现细节：跨面板编排全部集中在 main_window.py，各面板只发信号、收数据。
3. 「不可用」与「数值恰好是 0」必须能区分：monitor 每个数值字段都是 值|None，None 才显示「不可用」。
4. 一切失败都不抛异常到 UI：core 层所有对外函数都吞异常返回带中文 message 的结果对象，防止定时器回调崩掉事件循环。
5. 单一事实源：参数定义只在 params/registry.py；主题字符串常量只在 core/config.py；勾选状态的事实源是显卡驱动本身（不是控件缓存）。
6. 标准对话框按钮统一中文：main.py 启动时装 Qt 内置 qtbase_zh_CN.qm（不依赖系统区域），QMessageBox / QInputDialog / 文件对话框的标准按钮显示「是 / 否 / 确定 / 取消」——否则英文系统上会出现英文 Yes/No/OK，与全中文界面割裂。打包产物靠 spec 的 datas 携带 .qm，缺失时静默降级为系统区域。

---

## 2. 核心模块详解（core/）

### 2.1 config.py —— 配置与预设存储

| 概念 | 位置 / 规则 |
|---|---|
| 配置根目录 | %LOCALAPPDATA%/ninfer-launcher（用户级持久目录，与安装/构建位置解耦；LOCALAPPDATA 缺失时回落到用户主目录）。开发模式与打包版行为一致 |
| settings.json | last_preset / window_width / window_height / model_dir / exe_path / params（上次参数快照）/ theme。原子写入（先写 .tmp 再 os.replace） |
| presets/ | 每个预设一个 .json 文件，schema：{name, schema:1, model, port, params:{...}}。注意：model 和 port 既存在顶层也允许塞进 params，读取侧两处都看（兼容旧格式） |
| .deleted_builtin_presets | 无扩展名的墓碑文件，记录「已被用户删除的内置预设」名字列表 |

内置预设播种机制（容易踩坑的地方）：

- 随包分发 resources/presets/*.json（打包后落在 _internal/resources/presets，由 build.ps1 第 5 步额外拷一份到 exe 旁根目录保证第一候选命中）；
- 每次启动 seed_builtin_presets() 把「配置根里没有」的内置预设补进 presets/，绝不覆盖已有文件（用户改过的同名预设不会被出厂重置）；
- 用户删除内置预设时，名字记入墓碑文件，之后 seed 不再把它复活——删除是持久生效的。

预设名校验 check_preset_name()：落盘前的唯一守门。拦截空名、超过 50 字符、Windows 非法文件名字符（反斜杠/正斜杠/冒号/星号/问号/引号/尖括号/竖线）、以「.」结尾、保留设备名（CON/NUL/COM1…LPT9）。错误文案可直接弹给用户。

主题常量：THEME_DARK="dark" / THEME_LIGHT="light" / THEME_SYSTEM="system" 是本模块定义的唯一事实源，ui/theme.py、下拉框、Settings.theme 都只认这几个常量，谁都不另写字面量（防拼写漂移静默回落深色）。

### 2.2 ports.py —— 端口检测

- check_port(port)：用 SO_EXCLUSIVEADDRUSE 绑定 127.0.0.1 探测。EXCLUSIVEADDRUSE 让 bind 对 TIME_WAIT 中的端口也报占用（比 SO_REUSEADDR 语义更贴近「这个端口现在能不能 listen」）；
- suggest_port(preferred)：从 preferred+1 起最多扫 100 个候选，返回第一个空闲端口（当前 UI 未直接使用，留给将来自动换端口功能）；
- 越界端口返回 UNKNOWN 而不是 IN_USE。

### 2.3 health.py —— 就绪探测

- probe_health(host, port)：GET http://host:port/health，超时 3 s，永不抛异常；
- classify_health(code)：200→READY；503→NOT_READY(loading)；其他→NOT_READY(带详情)；
- HealthPoller：QObject + QTimer，周期 500 ms。信号 ready / not_ready(str) / aborted(str)。
  - abort_check 回调（main_window 传的是 process.abort_reason）：进程意外退出时中止探测并发出原因，避免「服务早退还在傻等健康检查」；
  - 探通即停表发 ready，由 main_window 调 process.mark_ready() 完成 STARTING→RUNNING。

### 2.4 process.py —— 进程生命周期（最复杂的模块，约 1000 行）

#### 四态状态机

```
STOPPED ──start()──▶ STARTING ──mark_ready()──▶ RUNNING
   ▲                    │                          │
   │                    ▼                          ▼
   └──────── STOPPED ◀── _finish_stop() ◀───── STOPPING
                     （STARTING/RUNNING 均可 stop() 进入 STOPPING）
```

合法迁移表 ALLOWED_TRANSITIONS 硬编码；任何非法迁移不会静默发生，而是打一条「内部状态异常」日志并保持原状态。

#### 输出处理

- stdout/stderr 分通道各自走 LineAssembler：字节流按换行切行（兼容 CRLF / CR），单行缓冲上限 8 KiB（超长强制断行防内存膨胀）；
- decode_output()：utf-8 → OEM codepage（GetOEMCP）→ locale 首选编码 的解码链，全部失败才 utf-8+replace。永不抛异常；
- 服务器输出行进入 tail（deque，保留最近 20 行），用于异常退出时的报错上下文。

#### 停止流程（逐级升级，本项目的核心难点）

Windows 上 CUDA 进程（ninfer-serve）处于 GPU 驱动临界区时会推迟甚至拒绝 TerminateProcess，Qt 的状态缓存和 taskkill 的返回值都可能「说谎」。因此停止流程的设计原则是：一切「进程已消失」的判定都以 OS 层 OpenProcess 探测（process_terminated）为准。

```
阶段 0  terminate()（Qt 软终止）
   │  宽限 5 s（TERMINATE_GRACE_SECONDS）内进程自行退出 → 直接收尾
   ▼ 仍存活
阶段 1  taskkill /PID <pid> /T /F（杀整个进程树）
   │  OS 探测仍存活
   ▼
阶段 2  强杀升级：每 1 s 重发 Win32 OpenProcess+TerminateProcess（绕过 Qt 直接打 PID），
        并以 OS 探测验证，直到进程真死或升级时限 15 s（KILL_ESCALATION_SECONDS）用尽
   │  时限用尽仍存活
   ▼
警告   打出显著日志「仍未退出：请在任务管理器中手动结束」——宁可带警告退出，
        也不静默留下占着 23 GB 显存的残留进程
```

- 同步路径 stop_and_wait()（关闭启动器时用）：上面三个阶段串行阻塞执行；
- 异步路径 stop()（点「停止」按钮）：terminate + 宽限计时器；宽限超时触发 taskkill；taskkill 后仍活则启动 _kill_verify_timer 周期性重发强杀 + OS 校验；
- 状态失同步兜底：若状态机说 STOPPED 但 OS 探测进程还活着（CUDA 进程被系统判「已结束」而实际没死），_begin_stop 强制收回停止流程，否则关闭启动器会把进程漏成孤儿；
- 显存回落等待 VramSettleWatcher：进程确认死亡后，每 0.25 s 读一次显存，基线下降 ≥128 MiB 判 SETTLED；3 s 超时判 TIMEOUT（降级继续）；无读数来源则固定等 0.5 s 判 DEGRADED。结果经 stop_finished 信号发出。

#### ExitInfo 分类

failed_to_start（程序不存在/不可执行）/ expected（主动停止）/ crashed / 正常退出码。意外退出时 format_lines() 会附上最后 20 行服务器输出，方便定位。

### 2.5 monitor.py —— 资源监视

三级来源判定（首次 poll 时做一次，之后缓存）：

1. NVML（pynvml）：首选，单次查询毫秒级；初始化失败或未安装 → 回落；
2. nvidia-smi：子进程跑 --query-gpu=index,name,memory.used,memory.total,clocks.sm,temperature.gpu,power.draw,power.limit --format=csv,noheader,nounits，超时 5 s，CSV 逐行宽松解析（[N/A]/空串→None）；刷新周期放宽到 3 s（每次要新起进程）；
3. UNAVAILABLE：两者都不可用，gpus 为空元组（不是填满 None 的假 GPU）。

特殊语义：

- NVML 运行期中途失效 ≠ 一开始就初始化失败：中途失效只当轮临时回落 nvidia-smi 交差并告知一次，下一轮仍先试 NVML；
- psutil.cpu_percent(interval=None) 首次调用必须先打底一次（其语义是「自上次调用以来的平均值」）；
- make_vram_reader(service, index=0)：产出与 process.py 的 VramReader 签名一致的闭包，把监视服务与「显存回落等待」对接起来（main_window 里 ServerProcess(vram_reader=make_vram_reader(monitor))）。

### 2.6 gpu_power.py —— NVIDIA 电源管理模式（NVAPI DRS）

把 NVIDIA 控制面板 → 管理 3D 设置 → 全局设置 → 电源管理模式 做成程序开关。对应 UI「性能模式」复选框。

- 开启 → 写 PREFER_MAX（最高性能优先，GPU 不在负载间隙降频）；
- 关闭 → 写 OPTIMAL_POWER（驱动出厂默认「正常」）。刻意不是「恢复你原先手动设过的档位」——那份状态一旦与驱动实际值不同步，恢复动作就会把用户的设置改成陈旧值且无任何迹象；
- 走 nvapi64.dll 的 DRS 接口：CreateSession → LoadSettings → GetBaseProfile（全局 base profile，不按 exe 建应用 profile）→ SetSetting → SaveSettings（漏了这一步改动只活在内存 session 里，重启全丢且返回码还是 0）；
- 写完回读核对：NVAPI 返回 0 不等于值真的落进了 profile，回读不符就报「未生效」；
- 结构体版本号用 sizeof(struct) | (version << 16) 现场计算，不许写死；NVDRS_SETTING 的字段一个都不能省（union 占位决定 sizeof）；
- 不在导入期加载 nvapi64.dll（无卡机器 import 就崩），每次调用时才加载；
- 顶层三函数 describe_availability / read_power_mode / apply_power_mode 一律不抛异常，返回带 ok/message 的 PowerModeResult。

为什么这个值不进 params/registry：registry 是 ninfer-serve 全部 CLI 参数的唯一事实源，混入非 CLI 项会顺着 get_values() 漏进预设文件和命令行。所以它是独立模块 + 独立开关控件。

---

## 3. 参数体系（params/）

### 3.1 六种参数种类与落参策略

| Kind | 落参策略 | UI 控件 | 示例 |
|---|---|---|---|
| INT | ALWAYS（None 不落） | HighlightedSpinBox / NullableSpinBox | port, max_context, prefill_chunk, draft_tokens, vision_max_tokens |
| FLOAT | ALWAYS | （当前无实例） | — |
| ENUM | ALWAYS（空串=不指定，不落） | HighlightedComboBox | kv_dtype, spec, reasoning_effort |
| BOOL3 | TRISTATE：UNSET 不落 / ON 落 flag / OFF 落 off_flag/off_value，都没有则不落 | Bool3CheckBox（两态） | lm_head_draft, no_thinking, vision |
| PATH | NON_EMPTY（空不落；model 是位置参数） | HighlightedLineEdit + 浏览按钮 | model |
| TEXT | NON_EMPTY | HighlightedLineEdit | — |

### 3.2 12 个参数清单（registry.py 唯一事实源）

| key | flag | 类型 | 默认值 | 分组 | 备注 |
|---|---|---|---|---|---|
| model | <model>（位置参数） | PATH | 空 | 服务基础 | .ninfer 模型文件，MTP 草稿头内置于模型 |
| port | --port | INT | 8080 | 服务基础 | 1~65535，启动前查占用 |
| kv_dtype | --kv-dtype | ENUM | rk4v4-e8 | 上下文与显存 | bf16/int8/rk8v4/rk4v4/rk4v4-e8/rk2v4-e8 |
| max_context | --max-context | INT | 163840 | 上下文与显存 | 调大直接增加 KV cache 显存 |
| prefill_chunk | --prefill-chunk | INT | 1024 | 上下文与显存 | 必须 128 的倍数（validate_values 专门检查） |
| spec | --spec | ENUM | mtp | 投机解码 | none / mtp |
| draft_tokens | --draft-tokens | INT | 7 | 投机解码 | 1~15；spec=none 时禁用且不落参 |
| lm_head_draft | --lm-head-draft | BOOL3 | on | 投机解码 | spec=none 时禁用 |
| no_thinking | --no-thinking | BOOL3 | off | 思考与推理 | on 时落 --no-thinking |
| reasoning_effort | --reasoning-effort | ENUM | low | 思考与推理 | 空/low/medium/xhigh；no_thinking=on 时禁用 |
| vision | --vision | BOOL3 | off | 视觉 | on 时落 --vision |
| vision_max_tokens | --vision-max-tokens | INT | None | 视觉 | default=None → NullableSpinBox；vision=off 时禁用 |

联动规则（disabled_when，builder 组装时跳过被禁用的参数，UI 同步置灰）：

- spec == "none" → draft_tokens、lm_head_draft 禁用；
- no_thinking == on → reasoning_effort 禁用；
- vision == off → vision_max_tokens 禁用。

### 3.3 命令行组装（builder.py）

build(values) 遍历 ALL_PARAMS：先查联动禁用，再按 EmitPolicy 落参。典型输出：

```
E:\ai\ninfer-4090-native\build-ninja\apps\ninfer-serve.exe E:\ai\ninfer-4090\qwen3_8_27b_8-19.ninfer --port 8080 --kv-dtype rk4v4-e8 --max-context 163840 --prefill-chunk 1024 --spec mtp --draft-tokens 7 --lm-head-draft --vision
```

注意 BOOL3 的 OFF 且无 off 形式 = 不落参（等于默认行为）；ENUM 空串 = 不指定不落参。

### 3.4 Bool3 序列化约定

UI 层的值域类型是 Bool3 枚举（Enum），但 JSON 只能存字符串。所有「值 → JSON」的路径（预设保存、settings.params 持久化）都必须先过 _param_value_to_config()（Bool3→on/off/unset 字符串），否则 json.dumps 抛 TypeError 导致静默失败（预设列表不刷新、settings 写不进去）。读取侧由 Bool3.from_config() 对称还原（接受 bool/str/None/Bool3 各种形态）。

---

## 4. UI 层详解（ui/）

### 4.1 main_window.py —— 编排中枢

构造顺序（有讲究）：

1. ConfigStore.open_default() → seed_builtin_presets() → load_settings()；
2. 创建 MonitorService（单例，同时供监视面板和进程层显存回落复用）→ ServerProcess(vram_reader=make_vram_reader(monitor)) → HealthPoller(abort_check=process.abort_reason)；
3. 创建三个面板 + GpuPowerSwitch（reader/writer/probe 可注入，默认真实驱动实现）；
4. 连接全部信号槽（见下）；
5. 恢复上次参数（_restore_params：settings.params 优先，last_preset 预设覆盖其上）；
6. 启动时立即 gpu_switch.turn_off()：每次启动都把 NVIDIA 电源管理模式写回出厂默认——上次忘关、或中途自己去控制面板开过，都不该带进下一次运行。

信号拓扑：

```
ControlPanel.start_requested ──▶ _on_start：取参数→校验(validate_values+模型存在+端口)→build(args)→resolve_exe→process.start(exe,args)
ControlPanel.stop_requested  ──▶ process.stop()（异步停止）
process.state_changed ──▶ _on_state_changed：apply_state 同步按钮可用性；STARTING 时启动 HealthPoller（指向当前端口）；STOPPED 时停
health.ready ──▶ process.mark_ready()（STARTING→RUNNING）
health.aborted ──▶ 若仍在 STARTING 则 mark_ready()（进程还活着但健康检查放弃的边界情况）
process.exited ──▶ _on_exited：意外退出打日志；无论何种退出都 gpu_switch.turn_off()
closeEvent ──▶ 门控条件 = 状态机 active OR OS 层 is_alive()（双保险，见 test_close_kill）→ 确认框 → stop_and_wait() → 保存窗口几何/参数 → turn_off → monitor shutdown
```

_resolve_exe 三级优先级（修过一个真实 bug：只读 settings.exe_path 会漏掉 ControlPanel 构造期自动探测到的路径，因为那时信号还没连上）：

1. 控制面板实时值（界面显示的就是它）；
2. settings.json 持久化的 exe_path；
3. 自动探测：_find_project_root()/build-ninja/apps/ninfer-serve.exe（向上找含 build-ninja/ 的目录，冻结/开发两种模式都可靠）。

预设操作：

- 选择预设：有未保存改动先确认（对比 _preset_snapshot）；载入 params（排除 model/port）+ 同步模型下拉 + 端口；记 last_preset 落盘；
- 保存配置：覆盖当前选中预设（先 check_preset_name 守门）；
- 另存为：输入新名 → 校验 → 落盘 → 刷新列表并选中；
- 删除：确认框（内置预设会提示「删除后不会自动恢复」）→ delete_preset → 内置的记墓碑 → 清 last_preset → 刷新列表 → 若有下一个预设则 force 载入（跳过未保存弹窗）；
- 重置默认：12 项参数回出厂值，预设下拉置空。

**标准对话框按钮的中文化**：确认类对话框（未保存改动 / 确认删除 / 确认退出，QMessageBox.question 的 Yes/No）、警告与提示框（OK）、「另存为」输入框（OK/Cancel）、文件对话框（打开/取消）用的都是 Qt 标准按钮，文案由 Qt 运行时提供而非本仓库字符串。main.py 在建窗口前强制装上内置简体中文翻译（qtbase_zh_CN.qm，独立于系统区域），这些按钮统一显示「是 / 否 / 确定 / 取消」。打包产物依赖 ninfer-launcher.spec 的 datas 把 .qm 打进 _internal/PySide6/translations/，缺失时静默降级为系统区域（其余文案不受影响，它们本就硬编码中文）。翻译器必须在任何窗口创建之前安装（按钮文案在按钮构造时按已装翻译器查找），且由模块级引用保活。

### 4.2 control_panel.py —— 控制面板

响应式布局：宽度 ≥860 px 双列（左=通用设置+预设+服务器控制，右=显存预检+资源监视），<860 px 竖排单列（resizeEvent 里重排）。

- 通用设置组：主题下拉（深色/浅色/跟随系统）、性能模式开关（GpuPowerSwitch）、Exe 路径（编辑框+浏览）、模型文件（下拉+浏览，扫描 model_dir/*.ninfer）、端口（SpinBox 1~65535）；
- _auto_detect（构造期执行）：自动填 build-ninja/apps/ninfer-serve.exe；若 launcher/models/ 下有 .ninfer 文件则重建模型下拉并选中第一个（这就是为什么 models/ 目录里的模型会被自动发现）；
- 按钮状态矩阵（apply_state，2026-09 用户反馈后收紧）：

| 状态 | 启动 | 停止 | 打开 WebUI | 显示命令 |
|---|---|---|---|---|
| STOPPED | 可用 | 禁用 | 禁用 | 可用 |
| STARTING | 禁用 | 可用 | 禁用 | 可用 |
| RUNNING | 禁用 | 可用 | 可用 | 可用 |
| STOPPING | 禁用 | 禁用 | 禁用 | 禁用 |

- 显示命令：生成完整命令行（含引号转义空格的路径）弹只读对话框。注意用 QDialog 承载 QTextEdit 而非直接 exec QTextEdit——PySide6 6.11+ 移除了 QWidget.exec()，直接调会 AttributeError 被外层 except 吞掉，表现为「点了没反应」。

### 4.3 params_tab.py —— 参数面板

- ParamValueStore：参数值的单一存放点，valueChanged(key, value) 广播；值变化判定走 is_value_default（语义级：Bool3 归一比较、float 用 isclose、其余裸 ==）；
- 控件按 registry 的 group 分组生成（「服务基础」组被剔除——model/port 已在控制面板，不重复）；
- 动态 tooltip 三段式：① 落参预览（按当前值实际算出的命令行片段）② 参数说明 ③ 默认值；值一变就重算；
- 高亮：值≠默认值的控件变 accent 色加粗；accent 色来自 ThemeManager 调色板（深浅两套不同强调色），经 set_accent_provider 注入 widgets 层；
- 搜索框：实时过滤（名称/旗标/key 大小写不敏感）+ 回车定位第一个匹配（滚动居中 + 闪烁 600 ms）；
- 联动禁用：store.valueChanged → _apply_linkage 按 disabled_when 规则置灰控件。

### 4.4 gpu_power_switch.py —— 性能模式开关

六个隐性契约（文件头注释详述），核心三条：

1. 写驱动期间用布尔闸 _applying 屏蔽自己的 toggled 信号（不用 blockSignals——那会连带屏蔽 Qt 内部状态同步信号）；写失败回弹勾选状态时不能再次触发写入，否则一次失败变成来回写驱动的循环；
2. 勾选状态的事实源是显卡驱动：每次 refresh 重新读驱动，不缓存（用户可能刚去控制面板改过）；
3. 不可用时既禁用控件又把原因写进 tooltip（无 NVIDIA 卡 / 驱动过旧 / 权限不足三种情况处置方式不同，必须可见）；

turn_off() 是程序代为关闭的专用入口（启动器启动 / 服务退出或被停止 / 关闭启动器时调用），不走 toggled 信号路径，成功才同步勾选，失败照样把原因经 messageReady 交出去（main_window 转给日志区）。

### 4.5 monitor_panel.py / log_panel.py / theme.py

- MonitorPanel：不采集数据，只持有 MonitorService + QTimer；每轮 refresh 后按 snapshot.source.refresh_interval_ms 重设周期（NVML 1 s / 回落 3 s）；「刷新已降频」标注只要 source.degraded 就一直挂着；显存进度条三色阈值（<60% 绿 / <85% 黄 / 红）；
- LogPanel：只读 QPlainTextEdit（Consolas 9pt，上限 5000 块），stderr 红 / stdout 青 / launcher 黄 三通道着色，支持导出 txt；
- ThemeManager：深色/浅色调色板（ThemePalette dataclass，accent 色是高亮的唯一取色来源）；「跟随系统」读 HKCU 注册表 AppsUseLightTheme（DWORD，0 深 1 浅，bool 要先拦——True==1 会误判浅色）；样式表按主题整份重建不做增量，内容没变时跳过 setStyleSheet（避免全部控件重算样式）；resolved 属性每次访问重新判定（跟随系统模式下系统主题可能在运行期被切换）。

---

## 5. 完整运行流程

### 5.1 启动流程

```
双击 ninfer-launcher.exe（或 python main.py）
  │
  ├─ QApplication 创建（应用名 NInfer Launcher v1.0.0）+ 装 Qt 内置简体中文翻译
  │
  ├─ ConfigStore.open_default() → %LOCALAPPDATA%/ninfer-launcher/
  ├─ seed_builtin_presets()：把 resources/presets/ 里「配置根没有且未被墓碑记录」的预设补进来
  ├─ load_settings()：恢复窗口尺寸 / model_dir / exe_path / theme / params
  ├─ MonitorService() + ServerProcess(vram_reader=...) + HealthPoller()
  ├─ ControlPanel 构造：扫 model_dir 下的 *.ninfer 填模型下拉；_auto_detect 填 exe 路径
  ├─ 恢复上次参数（settings.params / last_preset 预设）
  ├─ gpu_switch.turn_off()：把 NVIDIA 电源管理模式写回出厂默认（结果落日志区）
  └─ 监视面板定时器开始（1 s 一轮 NVML 采集）
```

### 5.2 启动服务流程

```
点「启动」
  │
  ├─ 取参数：ParamsTab.get_values() + 控制面板的 model/port
  ├─ 校验：validate_values（枚举取值/整数范围/prefill_chunk 128 倍数）
  │        + 模型文件非空且 os.path.isfile（提前拦截 CreateFileW 找不到文件）
  │        + check_port（SO_EXCLUSIVEADDRUSE 探测占用）
  │   任一失败 → 弹「启动前校验失败」列出全部错误，不启动
  ├─ build(values) → 命令行参数列表（联动禁用参数自动跳过）
  ├─ _resolve_exe() 三级优先级解析 ninfer-serve.exe 路径，不存在则报错
  ├─ process.start(exe, args)：
  │    QProcess.start（工作目录 = exe 所在目录）→ 记住 PID（last_pid）
  │    状态 STOPPED → STARTING
  ├─ HealthPoller 开始每 500 ms 探 http://127.0.0.1:<port>/health
  │    200 → ready → process.mark_ready() → RUNNING（按钮矩阵切到运行态，WebUI 可用）
  │    进程意外退出 → abort_reason 中止探测 → 日志附最后 20 行输出
  └─ 监视面板持续刷新 GPU 显存/频率/温度/功耗 + CPU/内存
```

### 5.3 停止流程

```
点「停止」（异步）或 关闭启动器（同步 stop_and_wait）
  │
  ├─ 状态 → STOPPING；建 VramSettleWatcher 并 begin()（记下显存基线）
  ├─ QProcess.terminate()
  ├─ [同步] waitForFinished(5s) / [异步] 宽限计时器 5 s
  │    进程自行退出 → 跳到收尾
  ├─ 仍存活 → taskkill /PID <pid> /T /F（杀进程树）
  │    OS 探测仍存活 → 升级强杀：每 1 s 重发 Win32 TerminateProcess + OpenProcess 校验
  │    （异步：_kill_verify_timer 驱动；同步：while 循环）
  │    15 s 时限用尽仍存活 → 显著警告日志，继续收尾（不静默残留）
  ├─ 进程确认死亡 → 显存回落等待：
  │    每 0.25 s 读显存，下降 ≥128 MiB → SETTLED
  │    3 s 未到 → TIMEOUT（降级继续）
  │    无读数来源 → 固定等 0.5 s → DEGRADED
  └─ 状态 → STOPPED；stop_finished(settle)；日志报告显存释放量
```

### 5.4 关闭启动器流程

```
closeEvent：
  门控 = process.state.active OR process.is_alive()（OS 真值，双保险）
  ├─ 门控为真 → 确认框「服务器正在运行，退出将停止它」→ No 则取消关闭
  ├─ stop_and_wait()（完整三级升级链，见 5.3）
  ├─ 保存窗口几何 + settings.params（Bool3 已归一成字符串）
  ├─ gpu_switch.turn_off()（再关一次是安全的空写，覆盖「服务本就已停但性能模式开着」的情形）
  └─ monitor_panel.stop() + monitor.shutdown()（释放 NVML）
```

---

## 6. 配置与预设体系

### 6.1 文件布局（运行时，位于 %LOCALAPPDATA%/ninfer-launcher/）

```
%LOCALAPPDATA%/ninfer-launcher/
├── settings.json               应用偏好（主题/窗口/路径/上次参数快照）
├── .deleted_builtin_presets    已删除内置预设的墓碑名单（JSON 数组）
└── presets/
    ├── qwen38-27b-mtp.json     内置预设（首次启动播种而来，可编辑）
    └── <我的预设>.json           用户自建预设
```

仓库内的 launcher/settings.json 是开发期遗留的样例文件，运行时并不读取它（配置根固定在 LOCALAPPDATA）；仓库内的 launcher/presets/ 同理只是参考样本，真正的内置预设来源是 resources/presets/。

### 6.2 预设文件格式

```json
{
  "name": "Qwen3.8-27B MTP",
  "schema": 1,
  "model": "E:/ai/ninfer-4090/qwen3_8_27b_8-19.ninfer",
  "port": 8080,
  "params": {
    "kv_dtype": "rk4v4-e8",
    "spec": "mtp",
    "draft_tokens": 7,
    "lm_head_draft": "on",
    "max_context": 163840,
    "prefill_chunk": 1024,
    "no_thinking": "off",
    "reasoning_effort": "low",
    "vision": "on"
  }
}
```

- BOOL3 参数存字符串 on/off/unset（不是 true/false）；
- model/port 可以放顶层也可以塞进 params（读取时两处都查，兼容旧格式）；
- 新增参数后旧预设缺字段 → 走 registry 默认值（values.get(spec.key, spec.default)），向前兼容。

### 6.3 常用操作速查

| 操作 | 入口 | 效果 |
|---|---|---|
| 保存配置 | 预设组「保存配置」 | 当前参数覆盖写入选中预设 |
| 另存为 | 预设组「另存为」 | 输入新名（受文件名规则约束）新建预设 |
| 删除预设 | 预设组「删除」 | 删文件；内置预设额外记墓碑，永不再自动恢复 |
| 重置默认 | 预设组「重置默认」 | 12 项参数回出厂值，预设选择清空 |
| 切换预设 | 预设下拉 | 有未保存改动先确认；载入参数+模型+端口 |

---

## 7. 构建与打包

### 7.1 环境要求

- Python 3.10+（本机 3.14）
- PySide6 >= 6.6、nvidia-ml-py >= 12.535、psutil >= 5.9（requirements.txt）
- PyInstaller（build.ps1 会自动 pip install 缺失依赖）
- 目标机需有 NVIDIA 驱动（NVML/NVAPI 才有意义；没有也能跑，监视面板显示「不可用」、性能模式开关禁用并注明原因）

### 7.2 构建命令

```bat
:: 双击 build.bat 即可，等价于：
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build.ps1

:: 可选参数：
scripts\build.ps1 -Clean          :: 构建前删除 build/ 与 dist/ 旧产物
scripts\build.ps1 -SkipTests      :: 跳过单元测试（不推荐）
scripts\build.ps1 -Launch         :: 构建成功后启动产物做 5 秒冒烟测试
scripts\build.ps1 -Clean -Launch  :: 组合
```

build.ps1 七步流程：

1. 环境检查：python 版本 + PySide6/pynvml/PyInstaller 是否可 import，缺了就 pip install；
2. 单元测试：pytest tests -q（默认套件自动排除 realsystem 标记用例），不过则中止构建；
3. 清理旧产物（仅 -Clean）；
4. PyInstaller 按 ninfer-launcher.spec 构建 onedir 包（console=False 无控制台窗口；excludes tkinter/matplotlib/numpy/pandas/scipy 瘦身；hiddenimports=pynvml；datas=resources/ + qtbase_zh_CN.qm → _internal/PySide6/translations/，标准对话框按钮中文化用）；
5. 拷贝内置预设：把 resources/ 再拷一份到 exe 旁根目录（PyInstaller 6 的 datas 落在 _internal/ 下，这份保证 builtin_presets_dir() 的第一候选 root/resources/presets 命中，行为与开发模式一致）；
6. 校验产物：exe 存在 + 打印体积；
7. 冒烟启动（仅 -Launch）：Start-Process 跑 5 秒，进程存活即通过然后 Kill。

产物：dist\ninfer-launcher 整目录（ninfer-launcher.exe + _internal/ + resources/），整目录拷贝即可分发运行，无需装 Python。

### 7.3 开发模式

```bat
cd launcher
pip install -r requirements.txt
python main.py                    :: 直接跑源码
python -m pytest tests -q         :: 跑测试（默认排除 realsystem）
```

开发模式下 project_root() = launcher/ 目录；_find_project_root() 向上找到含 build-ninja/ 的项目根（E:\ai\ninfer-4090-native），从而自动探测到 build-ninja/apps/ninfer-serve.exe。

---

## 8. 测试策略

### 8.1 测试套件一览（tests/，共 20 个测试文件）

| 文件 | 覆盖对象 | 要点 |
|---|---|---|
| test_process.py | ServerProcess 状态机/日志装配/停止 | 注入 fake killer/terminator/terminated_check，时间尺度缩小 |
| test_close_kill.py | 关闭时 CUDA 进程残留 + is_alive 语义回归（2026-09 用户报告） | 真启 ping.exe 子进程做 OS 真值闭环；模拟「永远杀不死」「先拒后死」「状态失同步」三种场景；`TestIsAliveSemantics` 钉死 is_alive() 语义（True=活），防 process_terminated 返回值修复漏改取反的回归 |
| test_gpu_power.py | gpu_power 模块本体 | 用 real_gpu_power_functions fixture 还原真函数，但桩掉 gpu_power._Nvapi，不会碰真实驱动 |
| test_gpu_power_switch.py / test_control_panel_gpu_power.py | 开关控件与面板集成 | 注入假 reader/writer/probe |
| test_monitor.py / test_monitor_panel_ui.py | 三级来源判定/CSV 解析/面板渲染 | 注入假 nvml_module/smi_runner/psutil_module |
| test_health.py / test_ports.py | 健康探测/端口检测 | 纯逻辑为主 |
| test_config.py / test_config_extra.py | 配置读写/预设 CRUD/播种/墓碑/名称校验 | tmp_path 隔离 |
| test_builder.py / test_registry.py | 命令行组装/参数清单完整性 | EXPECTED_PARAM_COUNT=12 钉死 |
| test_theme.py / test_widgets.py | 主题解析/QSS 生成/控件高亮 | 注入 reader 模拟注册表 |
| test_show_command.py / test_delete_preset.py / test_save_as_preset.py / test_control_buttons_state.py | UI 交互回归 | monkeypatch resolve_config_root 指到 tmp_path，绝不动真实配置 |
| test_translations.py | 标准对话框按钮中文化（是/否/确定/取消） | 临时把 qtbase_zh_CN 装到会话 QApplication 上断言按钮文案，结束即拆除；文件缺失时静默降级不抛异常 |
| test_deep_review.py | 深度审查用例集 | 综合回归 |
| conftest.py | 会话级共享夹具 | 见下 |

另有 _repro_close_leak.py / _repro_real_serve_terminate.py 两个手动复现脚本（非正式测试）。

### 8.2 conftest.py 的两个关键机制

1. 会话级唯一 QApplication（autouse）：必须是 QApplication 而非 QCoreApplication（widgets 测试要构造真实 QWidget）；且 Qt 不允许先建 QCoreApplication 再建 QApplication（会致命断言卡死整个 pytest 进程）。conftest 保证 QApplication 总是最先建好。QT_QPA_PLATFORM 用 setdefault 设为 offscreen（不覆盖外部显式指定的平台插件）；
2. 全局拦截真实 GPU 驱动读写（autouse，per-test）：把 core.gpu_power 的三个顶层函数替换成有状态的假实现（记住写进去的档位让读取跟着变——恒定返回值会让「开关点击后状态是否跟着变」的用例即使实现坏了也照样通过）。替换打在模块属性上，而 GpuPowerSwitch 构造时才取函数引用，所以能覆盖全部未显式注入的调用方。需要验证真实驱动交互的用例显式标 realsystem（pytest.ini 默认 -m "not realsystem" 排除，复跑时手动加 -m realsystem）。

### 8.3 运行测试

```bat
cd launcher
python -m pytest tests -q                 :: 默认套件（不含 realsystem）
python -m pytest tests -m smoke           :: 快速验证集
python -m pytest tests -m realsystem      :: 真实驱动核对（会读写本机显卡全局设置！谨慎）
```

---

## 9. 常见问题排查（FAQ / Troubleshooting）

### 9.1 启动相关

| 症状 | 原因 | 处置 |
|---|---|---|
| 「找不到服务器程序」 | ninfer-serve.exe 不在预期位置 | 在控制面板「Exe 路径」手动填完整路径（会持久化）；或确认项目根的 build-ninja/apps/ 下有编译产物 |
| 「模型文件不存在」 | 下拉框选的 .ninfer 路径无效 | 重新选模型文件；确认 model_dir 设置正确（settings.json 的 model_dir 字段） |
| 「端口 X 已被占用」 | 另一个进程（可能是上次残留的 serve）占着端口 | 任务管理器杀掉残留 ninfer-serve.exe；或换个端口 |
| 启动后一直「加载中」不转「运行中」 | 服务进程起了但 /health 一直 503（模型加载慢）或进程早退 | 看日志页最后 20 行输出定位；模型文件损坏/显存不足都会这样 |
| 服务就绪前退出 | 常见：显存不够装不下权重+KV cache | 调小 max_context；换低精度 kv_dtype（如 int8→rk4v4-e8） |

### 9.2 停止 / 关闭相关（本项目历史上最痛的点）

| 症状 | 原因 | 处置 |
|---|---|---|
| 关闭启动器后任务管理器还有 ninfer-serve.exe | CUDA 进程在 GPU 驱动临界区拒绝终止，15 s 强杀升级时限用尽 | 日志里会有显著警告「仍未退出：请在任务管理器中手动结束它」——照做即可。这是已知平台限制（WDDM 驱动态），代码已做到「宁可带警告也不静默残留」 |
| 停止后显存迟迟不降 | 显存回落等待超时（TIMEOUT）属正常降级，不影响后续启动 | 日志会显示「等待 3.0 秒仍未见显存回落（降级继续）」；若长期不降多半是驱动问题 |
| 日志出现「检测到状态失同步」 | 状态机说 stopped 但 OS 探测进程还活着 | 无需处理，代码已强制收回停止流程补杀，这是防御性修复 |
| 点「停止」后 ninfer-serve.exe 仍占显存、日志反复「外部服务在运行但无法定位其进程（无 PID 登记表）」 | is_alive() 语义曾反转：进程活着时被误判为「已不在运行」，`_begin_stop` 走「子进程已不在运行」分支、跳过全部杀进程步骤（terminate→taskkill→强杀）；GUI 启动又不写 PID 登记表，对账无法兜底 | 已修复（is_alive 取反，2026-09）。若再现：先确认 ninfer-serve.exe 是否残留并手动结束，再复现定位 |

### 9.3 监视 / 性能模式相关

| 症状 | 原因 | 处置 |
|---|---|---|
| 监视面板全「不可用」 | NVML 和 nvidia-smi 都不可用（无卡/驱动未装/driver 太旧） | 更新 NVIDIA 驱动；确认 nvidia-smi 能在 cmd 里跑通 |
| 「来源：nvidia-smi（已降级）（刷新已降频）」 | NVML 初始化失败，回落到子进程查询 | 重装/更新驱动让 pynvml 可用；降级期间功能完整只是刷新变慢（3 s） |
| 性能模式开关灰色不可点 | NVAPI 不可用（tooltip 里有具体原因：非 Windows / nvapi64.dll 缺失 / 函数 ID 解析不出） | 按 tooltip 提示处理；修改驱动设置通常需要管理员权限 |
| 勾选性能模式后回弹并报错 | 写驱动失败（权限不足最常见） | 用管理员身份运行启动器重试 |
| 关闭性能模式后发现档位不是自己之前手设的值 | 设计如此（契约 8）：关闭 = 写回驱动出厂默认「正常（最佳功耗）」，不是恢复你原先的档位 | 如需特定档位请去 NVIDIA 控制面板手动设置 |

### 9.4 配置 / 预设相关

| 症状 | 原因 | 处置 |
|---|---|---|
| 删掉的内置预设下次启动又回来了 | 不该发生——删除时已记墓碑。若发生，检查 %LOCALAPPDATA%/ninfer-launcher/.deleted_builtin_presets 是否存在且包含该名字 | 手动把名字加进该 JSON 数组 |
| 「保存预设失败：预设名称含非法字符…」 | 预设名要落成文件名 stem | 按提示改名（避开 Windows 非法文件名字符、保留设备名、≤50 字符、不以点结尾） |
| 预设列表不刷新 / settings.json 没更新（历史 bug 形态） | Bool3 枚举没归一成字符串就 json.dumps 抛 TypeError 被吞 | 已修复：所有落盘路径统一过 _param_value_to_config()。若再现，检查是否有自定义代码绕过了这条路径 |
| 换了台机器后预设丢了 | 预设在 %LOCALAPPDATA%，随用户账户走 | 把新机器的 %LOCALAPPDATA%/ninfer-launcher/ 整个目录拷过去（或重新用「另存为」建） |
| 想完全重置启动器状态 | — | 删掉 %LOCALAPPDATA%/ninfer-launcher/ 整个目录（下次启动会重新播种内置预设） |

### 9.5 打包 / 部署

| 症状 | 原因 | 处置 |
|---|---|---|
| 打包后内置预设不见了 | build.ps1 第 5 步的 resources 拷贝没执行成功 | 确认 dist/ninfer-launcher/resources/presets/ 下有 .json；重跑 build.ps1 -Clean |
| 打包后 exe 双击闪退 | 缺 VC++ 运行库 / 目标机环境问题 | 装 VS2022 运行库 redistributable；用命令行方式跑 dist 里的 exe 看首屏报错 |
| 打包版对话框按钮显示英文 Yes/No | spec 的 datas 丢了 qtbase_zh_CN.qm 条目（或构建机 PySide6 缺该文件），产物里没有 _internal/PySide6/translations/ | 重跑 build.ps1 并确认 spec 的 _datas 含该条目 |
| 目标机没有 NVIDIA 卡 | 设计上支持：监视面板显示「不可用」、性能模式开关禁用并注明原因，其余功能正常 | 无需处理 |

---

## 10. 关键常量速查表

| 常量 | 值 | 含义 |
|---|---|---|
| TERMINATE_GRACE_SECONDS | 5.0 | terminate 后的宽限期 |
| TASKKILL_TIMEOUT_SECONDS | 10.0 | taskkill 子进程超时 |
| KILL_ESCALATION_SECONDS | 15.0 | 强杀升级阶段总时限 |
| KILL_CHECK_INTERVAL_MS | 1000 | 强杀升级的重发/校验周期 |
| VRAM_SETTLE_TIMEOUT_SECONDS | 3.0 | 显存回落等待超时 |
| VRAM_POLL_INTERVAL_SECONDS | 0.25 | 显存回落轮询间隔 |
| VRAM_FALLBACK_WAIT_SECONDS | 0.5 | 无读数来源时的降级固定等待 |
| VRAM_SETTLE_DROP_BYTES | 128 MiB | 判「已回落」的显存下降阈值 |
| TAIL_LINE_COUNT / LINE_BUFFER_LIMIT | 20 / 8192 | 日志尾部行数 / 单行缓冲上限 |
| HEALTH_INTERVAL_MS / HEALTH_TIMEOUT_SECONDS | 500 / 3.0 | 健康探测周期 / 单次超时 |
| NORMAL_INTERVAL_MS / FALLBACK_INTERVAL_MS | 1000 / 3000 | 监视刷新周期（NVML / nvidia-smi 回落） |
| NVIDIA_SMI_TIMEOUT_SECONDS | 5.0 | nvidia-smi 子进程超时 |
| SUGGEST_PORT_LIMIT | 100 | 端口顺延扫描上限 |
| PREFERRED_PSTATE_ID | 0x1057EB71 | NVAPI「电源管理模式」设置 ID |
| ENABLE_MODE / DISABLE_MODE | PREFER_MAX(1) / OPTIMAL_POWER(5) | 性能模式开/关写入的档位 |
| EXPECTED_PARAM_COUNT | 12 | registry 参数条目数（钉死防漏） |
| _BREAKPOINT (control_panel) | 860 px | 双列/单列布局切换断点 |

---

## 11. 维护者备忘（改动前必读的陷阱清单）

1. process.py 的停止流程：任何改动都必须保持「OS 层 OpenProcess 探测是唯一真值」的原则；不要信任 Qt 状态缓存或单次 kill 返回值。test_close_kill.py 的三个场景（永远杀不死 / 先拒后死 / 状态失同步）是回归底线。
2. gpu_power.py 的结构体：字段一个都不能省、版本号必须现场算（契约 1/2）；SetSetting 之后必须有 SaveSettings（契约 3）；session/NVAPI 必须成对释放（契约 4）。
3. gpu_power_switch 的信号闸：用布尔闸 _applying 而非 blockSignals（契约 1）；import 的是模块而不是函数（否则 monkeypatch 拦不住）。
4. Bool3 序列化：任何新的「参数值 → JSON」路径都必须过 _param_value_to_config()，否则静默失败。
5. registry 契约：新增参数 = 加一条 ParamSpec + 同步 EXPECTED_PARAM_COUNT；非 CLI 的设置（如电源模式）绝不放进 registry。
6. config 的主题常量：只在 core/config.py 定义，别处一律 import 引用，不写字面量。
7. conftest 的两个 autouse 夹具：QApplication 必须最先建（Qt 单例限制）；GPU 驱动读写必须在会话级拦掉（否则测试会真的改本机显卡全局设置，且没有任何 diff 能暴露这件事）。
8. PySide6 版本差异：QMessageBox.question 返回 int 要用 == 比较（is 永远 False）；QWidget.exec() 在 6.11+ 被移除，模态对话框一律用 QDialog 承载。
9. 打包：PyInstaller 6 onedir 的 datas 落在 _internal/ 下，build.ps1 的第 5 步补拷是 builtin_presets_dir() 第一候选命中的保证，删不得。
10. 测试纪律：默认套件绝不碰真实显卡驱动（realsystem 标记 + pytest.ini 默认排除）；UI 测试的配置根一律 monkeypatch 到 tmp_path。
11. 标准按钮中文化：qtbase_zh_CN.qm 声明在 ninfer-launcher.spec 的 datas 里（→ _internal/PySide6/translations/），删了它打包版对话框按钮就回落系统区域（英文环境下重新出现英文 Yes/No，其余功能不受影响）。main._install_chinese_translation 的候选目录顺序是 sys._MEIPASS（打包）在前、PySide6.__file__（开发）在后——打包版里 PySide6.__file__ 指向 PYZ 压缩包内部，其父目录在磁盘上不存在，别「简化」成只留后者。翻译器必须在任何窗口创建前安装，模块级 _TRANSLATOR 保活（QTranslator 被 GC 翻译即失效）。
