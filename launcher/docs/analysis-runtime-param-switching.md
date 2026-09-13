# 启动器运行时参数热切换能力分析

> 分析日期：2026-07-14 · 范围：`launcher/`（PySide6 GUI）+ `ninfer-serve`（C++ HTTP 服务）+ DSH（DeepSeek Harness）模型能力发现链路

## 问题背景

用户提出三个关联问题：

1. 启动器是否支持**不重启服务**就切换思考等级（reasoning effort）和视觉模式（vision）？
2. 为什么 DSH 的 DeepSeek 路由能在会话中调整思考等级，而本地 ninfer-serve 路由不行？
3. 同一会话内多次提问使用不同思考等级，对质量影响大吗？

---

## 一、启动器能否热切换？——不能

### 1.1 启动器的架构决定了这一点

启动器是一个 PySide6/Qt GUI，把 `ninfer-serve.exe` 作为**子进程**管理，内部是四态状态机（STOPPED → STARTING → RUNNING → STOPPING）。所有参数在**进程 spawn 时一次性固化为 CLI 参数**，运行期间没有任何重新配置通道：

| 组件 | 文件 | 职责 |
|------|------|------|
| 参数注册表 | `params/registry.py` | 12 个 ParamSpec（含 `no_thinking`、`reasoning_effort`、`vision`、`vision_max_tokens`），`EXPECTED_PARAM_COUNT = 12` |
| 命令构建 | `params/builder.py` | 纯函数：值 → argv；Bool3 三态（on/off/unset）按 TRISTATE 策略发射 |
| 进程管理 | `core/process.py` | `ServerProcess` QProcess 封装，**无运行中重配置路径** |
| 主窗口 | `ui/main_window.py` | ServerProcess / HealthPoller / ConfigStore 的唯一持有者；closeEvent 保存参数并停止进程 |
| 参数页 | `ui/params_tab.py` | `ParamValueStore` 单一值存储 + `valueChanged` 信号；控件只影响存储值和命令预览 |
| 健康轮询 | `core/health.py` | 每 500 ms 轮询 `/health`，仅用于 STARTING→READY 判定 |

关键结论：**GUI 上修改任何参数只会更新"下次启动的命令预览"，不会触达正在运行的服务**。测试套件（`tests/test_builder.py`、`test_deep_review.py` 等）也只断言 flag 发射与联动禁用关系，不存在任何运行时切换测试。

### 1.2 服务端侧的事实（更细一层）

即使绕过启动器直接操作 HTTP API，两个参数的命运也不同：

#### 思考等级（thinking / reasoning effort）——服务端**支持**逐请求覆盖

- `src/serve/translate.cpp` 的 `resolve_prompt_semantics()`（约 109–175 行）：请求体中的 `reasoning_effort` **优先于**服务器默认值（CLI 的 `--reasoning-effort`），并与 `enable_thinking` 做冲突检查和能力校验。
- OpenAI 协议：`parse_openai_reasoning_effort()`（`openai_schema.cpp:536-549`）接受 `none|minimal|low|medium|high|xhigh`；其中 `minimal/high/max` 会被模板能力检查拒绝（400 `reasoning_effort_not_supported`），因为加载的 Qwen 模板只支持 **low / medium / xhigh**（默认 XHigh）。
- Anthropic 协议：`parse_thinking()`（`anthropic_schema.cpp:421-433`，`type != "disabled"` → 开）+ `output_config.effort` 映射到 `reasoning_effort`。
- 模板语义（`chat_template.cpp`）：effort 通过向 system block 注入指令文本实现 —— low → "Keep your thinking brief…"，medium → 无注入，xhigh → "Please think carefully…"（`kLowReasoningInstructions` / `kXHighReasoningInstructions`，第 28–35 行）。
- 但注意：HTTP API **没有任何 PUT/PATCH/config 端点**可以改服务器级默认值；能变的只有"每次请求带什么"。

#### 视觉模式（vision）——服务端**架构性冻结**，只能重启

- `--vision` 在 `serve_options.cpp` 解析后进入 `EngineOptions.enable_vision`；`generation_service.cpp` 构造函数把它烤进 engine options（286–287 行），派生出 `StartupFeatures{vision, vision_max_tokens, ...}`（`startup_features.h`）。
- 它影响 CUDA graph / layout 规划（`layouts_impl.h` 256、400–404、531–533、642–644 行）、frontend 构造（`package.cpp:112-117` 用 `runtime.features.vision` 建 frontend）、以及不可变的 `Frontend::vision_enabled`（`frontend.cpp:648`）。
- 运行期若收到带媒体的请求而服务未启用 vision，返回 `vision_disabled` 错误（`generation_service.cpp:376-378, 416-418`）；`prepare()` 抛 "Vision is disabled for this Engine"（`frontend.cpp:881-883, 936-938`）。
- **off→on 和 on→off 都要求重启进程**，没有例外。

### 1.3 小结

| 设置 | 启动器 GUI | 直接打 HTTP API | 根因 |
|------|-----------|----------------|------|
| 思考等级 | ❌ 仅下次启动生效 | ✅ 每请求 `reasoning_effort` 字段可覆盖 | 服务端有 per-request override 通道，但 launcher 没暴露它 |
| 视觉模式 | ❌ 仅下次启动生效 | ❌ 完全冻结（CUDA graph/layout/frontend 全部启动期决定） | 架构性冻结 |

---

## 二、为什么 DSH 的 DeepSeek 路由有思考等级选择器，本地路由没有？

### 2.1 DSH 的能力发现机制

DSH 的 ACP 层（`.packages/acp/acp/src/model-control.ts`）只在 LLM adapter 的 `resolveModelInfo()` 返回了 `reasoning` 字段时才发出 `reasoning_effort` 配置项。**UI 控件的可见性完全由 adapter 声明的元数据驱动，而不是远端服务器自报的能力。**

- **`llm-deepseek`**（adapter.ts:390-428）：**硬编码** `REASONING_EFFORTS = off/low/high/max`（当 `defaults.thinking === 'disabled'` 时退化为 off-only），serialize 层把 effort 映射到 wire 上的 `reasoning_effort` / `thinking:{type:'disabled'}`。所以 DeepSeek 路由永远显示选择器。
- **`llm-pi-ai`**（catalog.ts:657-710）：从已安装 catalog 条目的 `reasoningEfforts:` 字典推导 `thinkingLevelMap`（未声明的 level 钉为 null；空字典或 null → `invalid(...)` 错误）；字段缺省时回落到 installed base entry。adapter.ts:187-194 的 `reasoningInfo()`：`if (!model.reasoning) return {}` —— **没有能力声明就没有 UI 控件**。

### 2.2 本地 ninfer-serve 路由为什么没有

两件事同时成立：

1. **服务端零广告**：`GET /props`（`http_server.cpp:293-333`）只发布 `modalities.{vision,video,audio}` + 采样默认值 + 端点标志；`GET /v1/models`（`make_models_list`，`openai_schema.cpp:720-736`）只返回 id/name/context_window/modalities.vision。**没有任何 reasoning 能力字段**。
2. **DSH 侧无条目**：pi-ai catalog 里没有这个本地模型的条目，profile 里也没写 `reasoningEfforts:`，于是 `resolveModelInfo().reasoning === undefined` → 不出控件。

讽刺的是：服务端**实际支持** per-request `reasoning_effort`（见 1.2），只是双方都没把这个事实接起来。

### 2.3 三种修复路径

| 方案 | 改动量 | 说明 |
|------|--------|------|
| (a) DSH profile 声明能力 | 零代码 | 在 llm-pi-ai 的用户 profile 里给该模型加 `reasoningEfforts:` 字典（必要时配 `compat` 重塑 wire 拼写）。最快落地。 |
| (b) 服务端自报 + DSH 读取 | 双侧小改 | ninfer 的 `/props`/`/models` 增加 reasoning 字段 + DSH 一个小 adapter 读它。最"正确"但要动两边。 |
| (c) 绕过 UI 直发字段 | 零改动 | 客户端直接在请求体塞 `reasoning_effort`（服务端本来就认）。适合脚本/自动化场景。 |

⚠️ 词汇表注意：ninfer 接受 `none,minimal,low,medium,high,xhigh`，但模板只认 `low/medium/xhigh`（minimal/high/max → 400）。DSH deepseek adapter 用的是 off/low/high/max —— 为 ninfer 写自定义 profile 时应按 low→low、medium→medium、xhigh→xhigh 映射，避免踩 400。

### 2.4 已落地（方案 a）

在用户 profile `~/.dsh/settings.yaml` 的本地模型条目上声明了能力（零代码改动）：

```yaml
llm-pi-ai:
  providers:
    ollama:                      # 该路由实际指向 ninfer-serve (http://localhost:8080/v1)
      api: openai-completions
      models:
        - id: Qwen3.8-27B
          reasoningEfforts:
            off: none            # wire "none" → 服务端逐请求关闭 thinking
            low: low
            medium: medium
            xhigh: xhigh         # minimal/high/max 未声明 → 钉为 null，UI 不展示
```

链路验证：`resolveModelReasoning()` 生成 `thinkingLevelMap{off:"none",low,medium,xhigh}` → pi-ai 0.85.1 openai-completions 默认分支把选中档位以 `reasoning_effort: <wire>` 写入请求体（off 档发 `"none"`）→ ninfer `resolve_prompt_semantics()` 逐请求覆盖服务器默认值，`None` 关思考、Low/Medium/XHigh 直通模板。重启 DSH Web（或新开会话）后，会话设置里即出现思考等级选择器。

---

## 三、同一会话内切换思考等级的质量影响

### 3.1 机制上是干净的

每个 turn 独立走 `resolve_prompt_semantics()`，请求值经能力校验后生效，不存在未定义行为或跨 turn 状态污染。effort 差异只体现在**当前 turn 的 system block 头部指令文本**上。

### 3.2 真正的两个代价

1. **历史 CoT 保留**：effort 模板下 `keep_thinking = preserve_thinking || (i > last_query_index)`（`chat_template.cpp:401-402`），默认 `preserve_thinking=true` 会把之前所有 turn 的长 reasoning_content 原样留在上下文里。后果：token 膨胀 + 风格漂移（前面 xhigh 的冗长推理会锚定后续 low 档的回答风格）。想干净切换可在请求里设 `preserve_thinking:false`，清掉旧 turn 的思考内容。
2. **前缀缓存失效**：effort 指令改变了渲染后的 system block 头部字节 → 每次切换档位都会击穿 prefix cache，整段 prefill 重新付钱。频繁来回切档比一直用高档更贵（低档本身省的是生成 token，省不回 prefill）。

### 3.3 实践建议

- 按任务分段固定档位（例如"探索阶段 low、攻坚阶段 xhigh"），而不是逐条消息抖动。
- 需要降档继续长会话时，配合 `preserve_thinking:false` 控制上下文体积。

---

## 附：关键文件索引

**Launcher**
- `ninfer_launcher/params/registry.py` — 参数定义与联动（G_REASONING / G_VISION 组）
- `ninfer_launcher/params/builder.py` — 值→argv 一次性发射
- `ninfer_launcher/core/process.py` — 子进程生命周期，无热更通道
- `ninfer_launcher/ui/main_window.py` — 状态机与资源持有者
- `presets/qwen38-27b-mtp.json` — 内置预设（no_thinking=off, reasoning_effort=low, vision=on）

**ninfer-serve（C++）**
- `src/serve/serve_options.cpp` — CLI 解析（`--vision` / `--no-thinking` / `--reasoning-effort`）
- `src/serve/generation_service.cpp` — vision 烘焙进 engine options；per-request 语义解析入口
- `src/serve/http_server.cpp` — 路由表；`/props`（293–333）无任何 reasoning 字段
- `src/serve/translate.cpp` — `resolve_prompt_semantics()` 请求值优先逻辑
- `src/serve/openai_schema.cpp` — `parse_openai_reasoning_effort`（536–549）、`make_models_list`（720–736）
- `src/targets/qwen3_6/impl/frontend/chat_template.cpp` — effort→指令文本映射（221–245）、能力集（293–302）、历史 CoT 保留（401–411）
- `src/targets/qwen3_6/export/ninfer/targets/qwen3_6/startup_features.h` — 启动期特性快照

**DSH**
- `.packages/acp/acp/src/model-control.ts` — 配置项仅在 adapter 报告 reasoning 能力时发出
- `.packages/llm/llm-deepseek/src/adapter.ts` — 硬编码 REASONING_EFFORTS
- `.packages/llm/llm-pi-ai/src/catalog.ts` — `reasoningEfforts` 字典 → thinkingLevelMap
- `.packages/llm/llm-pi-ai/src/adapter.ts` — `reasoningInfo()`：无能力则无 UI
