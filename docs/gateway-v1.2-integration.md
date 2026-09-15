# knowledge-base-server 对接 knowledge-base-gateway v1.2 指南

本文是 `gateway-integration.md`（v1.1 对接文档）的升级篇。v1.1 的安装、
`.env`、key 获取、`KB_CHAT_*` 配置和网络拓扑**全部不变**，本文只讲 v1.2
带来的增量：工具调用透传、`/v1/responses` 协议、模型能力发现、流式配额
结算，以及升级时的一个必须操作（catalog 能力声明）。

> v1.1 文档中的重要兼容性说明已在 v1.2 解除：Gateway 现在完整支持 OpenAI
> `tools` / function calling 透传。QA agent 的 `search_knowledge` 工具循环
> 可以直接通过 Gateway 运行，`sources`、引用和检索质量从此可以纳入端到端
> 验收，不再需要绕过 Gateway 直连模型服务。

## 1. v1.2 相对 v1.1 的变化（server 视角）

| 能力 | 对 server 的意义 |
| --- | --- |
| Chat Completions 支持 `tools` / `tool_choice` / `response_format` | pydantic-ai 的工具循环（`search_knowledge` 等）原样可用，`KB_CHAT_*` 配置零改动 |
| `POST /v1/responses` | 新的 Responses 协议（可选采用）：类型化 SSE 事件、原生工具形状、`text.format` 结构化输出 |
| `GET /v1/models`、`/v1/models/{model}` | 启动时发现当前 subject 可用的模型和**公开能力矩阵**，替代硬编码假设 |
| 流式 usage 结算配额 | 流式请求的 usage 现在会计入 subject 的每日/每月 token quota（v1.1 只结算非流式） |
| 管理端指标增强 | `/admin/usage` 增加 `error_rate`、首字延迟百分位；`/admin/providers` 增加 health/breaker 状态，便于运维排障 |
| 能力矩阵 catalog 门控 | 每个模型声明支持哪些能力（tools/responses/structured_output/stream…），不支持的能力在调用 provider **之前**被 400 拒绝 |

兼容性承诺：以上全部是 **V1 内的加法演进**。chat 的既有请求/响应形状、
SSE 终止符（`data: [DONE]`）、错误 envelope 和 request-id 行为保持冻结；
server 既有客户端**不需要任何修改**即可继续工作。已验证 openai-python
3.5.0 全部场景（chat 非流式/流式、responses 非流式/流式/原生 tools/原生
结构化输出）通过，无需 `extra_body` 变通。

## 2. 升级 Gateway

```bash
cd ../knowledge-base-gateway
git pull            # 或切到 v1.2 发布分支
```

**升级顺序**：先迁移数据库，再重启 Gateway 进程（Gateway 从不自动改 schema）：

```bash
# Compose 部署：一次性 migration job 已在 compose 内，up -d --build 即可
docker compose up -d --build
# 手动部署：
go run ./cmd/migrate up
go run ./cmd/migrate version    # 应显示 version 4 (dirty=false)
# 然后重启 gateway 进程
```

迁移 0003/0004 均为加法（新表 `admin_audit`、新列 `protocol`、
`first_token_millis`），回滚路径见各自的 `.down.sql`。回滚 Gateway 二进制
不需要回滚数据库。

### 2.1 必须操作：为自建模型声明能力矩阵

**这是 v1.1 → v1.2 升级唯一的必做配置。** 能力矩阵存在
`model_catalog.capabilities`（JSONB）。v1.1 期间自建的模型行该字段是
`{"stream": true}` 或 `{}`——升级后这些模型的工具调用和 responses 会被
`400 capability_not_supported` 拒绝，**不会自动放开**。

参考声明（真实模型通常至少放开 chat/stream/tools/usage）：

```sql
UPDATE model_catalog
SET capabilities = jsonb_build_object(
        'chat', true,
        'responses', true,
        'stream', true,
        'tools', true,
        'structured_output', true,   -- 该模型支持 json_schema 输出时才开
        'json_mode', false,
        'vision', false,
        'reasoning', false,
        'usage', true,               -- 上游会报告 token usage（建议开）
        'context_tokens', 128000,    -- 按真实模型填写；超过会 400
        'max_output_tokens', 8192,
        'max_tools', 16
    ),
    config_version = config_version + 1
WHERE public_name = '<你的公开模型名>';
```

字段语义：`context_tokens` / `max_output_tokens` 是请求侧上限（输入估算或
`max_tokens` 超限直接 400）；`max_tools` 是单请求工具数上限；`usage` 为
false 时该模型的 usage 视为未知，配额按保守预扣结算。种子模型
`gateway-echo` 已由迁移 0003 自动声明全矩阵，仅供联调。

验证：

```bash
curl -s -H "Authorization: Bearer $KB_GATEWAY_KEY" \
  "$GATEWAY_URL/v1/models/<你的公开模型名>"
```

响应中的 `capabilities` 应与声明一致；该接口只返回当前 subject 有权使用的
模型，provider 名、上游模型名、URL 永不暴露。模型不可用（不存在/禁用/
未授权）统一返回 403 `model_not_allowed`，不做区分。

## 3. 工具调用对接（QA agent 主路径）

server 的 chat 客户端（`src/app/llm/models.py`，pydantic-ai
`OpenAIChatModel` over `AsyncOpenAI`）**不需要任何代码或配置改动**。
pydantic-ai 发送的是嵌套 chat 形状的 function tools，Gateway 原样透传给
上游，工具调用循环（`tool_calls` → server 执行 `search_knowledge` →
`tool` 消息回传）完全在 server 与 Gateway 之间工作。

行为约定：

- 工具数超过该模型 `max_tools`、工具 schema 超限或重复命名 → 400
  `invalid_request`，不触碰 provider；
- 流式请求一旦开始输出，失败只表现为截断流或终止错误事件，Gateway 不会
  在输出后切换上游或重试；
- 工具调用参数的组装错误在流内以协议错误呈现，并在 Gateway 审计中记录为
  `schema_validation_failed` / `invalid_tool_arguments` 类（不含内容）。

### 3.1 用 fake provider 冒烟工具链路

dev 模式的 `gateway-echo` 是确定性 mock：带 tools 的请求会返回一次对第一个
工具的调用（参数 `{"input": "<最后一条文本>"}`）；带工具结果的请求回答
`tool ok: <content>`。可以用它验证 server 的工具循环不含 Gateway 侧障碍：

```bash
cd ../knowledge-base-gateway
GATEWAY_API_KEYS="kb-local:kb-server:sk-kb-local" \
GATEWAY_MODELS="gateway-echo:fake:echo-model" \
GATEWAY_PROVIDER=fake go run ./cmd/gateway
```

```bash
cd ../knowledge-base-server
KB_CHAT_BASE_URL=http://127.0.0.1:8080/v1 \
KB_CHAT_API_KEY=sk-kb-local KB_CHAT_MODEL=gateway-echo \
uv run uvicorn app.main:app --port 8000
curl -N -sS http://127.0.0.1:8000/api/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"question":"知识库里有关于部署的文档吗","limit":4}'
```

验收点：SSE 序列正常走完且 `done` 结束。此模式下的回答内容仍是 echo，
不代表检索质量；把 Gateway 指向真实模型后，`sources`、引用编号与
`search_knowledge` 的实际调用才应纳入端到端验收（这是 v1.1 文档遗留验收
项的正式启用条件）。

### 3.2 真实模型端到端

数据库模式下确认（见 v1.1 文档 3.2/3.3 获取 key）：provider 已配置且
secret 已注入、catalog 能力已按第 2.1 节声明、subject policy 已授权该模型。
然后将 `KB_CHAT_MODEL` 指向该公开模型重启 server，以「回答带 `sources` 且
引用编号可对应检索块」为验收标准。

## 4. 可选进阶：`/v1/responses`

新协议，与 chat 并存，互不影响。server 当前通过 pydantic-ai 走 chat 即可；
如果后续想采用 Responses（类型化事件、原生形状），SDK 只需：

```python
from openai import AsyncOpenAI   # 与现有 chat 客户端同一个 base_url/key

resp = await client.responses.create(
    model=settings.CHAT_MODEL,
    input=[{"role": "user", "content": "..."}],
    tools=[{"type": "function", "name": "search_knowledge",
            "description": "...", "parameters": {...}}],   # 原生扁平形状
    stream=True,
)
```

要点：

- 接受两种工具写法（嵌套 chat 形状 / 原生扁平形状）和两种结构化输出写法
  （`response_format` / `text.format`，二者互斥）；接受字段是显式清单，
  **未知顶层字段会被 400 拒绝**（chat 则忽略未知字段，行为与 v1.1 一致）；
- 稳定 SSE 事件：`response.created` → `response.output_text.delta` /
  `response.function_call_arguments.delta` → `response.completed` 或
  `response.failed`（二者恰其一收尾）；
- 独立回滚开关：`GATEWAY_RESPONSES_ENABLED=false` 可单独关闭该端点；
- 结构化输出按模型能力矩阵门控（Anthropic 上游暂未支持，保持
  `structured_output: false` 即可）。

## 5. 配额、错误与重试

- 流式请求的 usage 现在会结算进每日/每月 token quota（结算恰一次；上游
  未报告 usage 时保留保守预扣）。server 侧对 429 的处理与 v1.1 相同：
  `rate_limit_exceeded` / `quota_exceeded` 均带 `Retry-After`，配额在下一个
  UTC 日/月边界恢复，不要在配额窗口内重试。
- v1.2 新增错误码：400 `capability_not_supported`（模型能力矩阵未放开所
  请求能力——先查第 2.1 节的声明，而不是重试）；流内
  `invalid_tool_arguments` / `schema_validation_failed`（见第 3 节）。
- 完整状态码→处理建议表见 v1.1 文档第 8 节和 Gateway 仓库
  `docs/developer-quickstart.md` 第 4 节，两者一致。
- 继续为每个请求带 `X-Request-ID`（server 已有约定）：它同时出现在 Gateway
  审计、指标和响应头中，是跨端排障的主键。

## 6. 运维观测（可选）

管理员 token 不变（见 v1.1 文档 3.2）：

```bash
curl -s -H "Authorization: Bearer $GATEWAY_ADMIN_TOKEN" \
  "$GATEWAY_ADMIN_URL/admin/usage"      # requests/errors/error_rate/token、p50/p95 延迟、首字延迟百分位
curl -s -H "Authorization: Bearer $GATEWAY_ADMIN_TOKEN" \
  "$GATEWAY_ADMIN_URL/admin/providers"  # health(serving/degraded/disabled)、breaker 状态、24h 错误数
curl -s -X POST -H "Authorization: Bearer $GATEWAY_ADMIN_TOKEN" \
  "$GATEWAY_ADMIN_URL/admin/models/<model>/disable"   # 原子切换、立即生效、有管理审计
```

指标均为元数据（错误分类，绝无 prompt/completion 内容）。`cost_micros`
暂为 `null`（定价配置未定，不是 0）；dev 模式的百分位是近似值，以
PostgreSQL 模式为准。模型禁用是热生效的：禁用后 server 侧该模型请求收到
403 `model_not_allowed`，无需重启任何进程。

## 7. 升级验收清单

- [ ] `go run ./cmd/migrate version` 显示 `version 4 (dirty=false)`；
- [ ] 自建模型的 `capabilities` 已声明，`/v1/models/{model}` 返回预期矩阵；
- [ ] fake 模式下 server 工具循环冒烟通过（第 3.1 节）；
- [ ] 真实模型下 QA 回答带 `sources` 且引用可对应（v1.1 遗留验收项启用）；
- [ ] 一次人为 429（调低 policy 限额）验证 server 按 `Retry-After` 退避；
- [ ] （如采用 responses）SDK 矩阵按 Gateway `docs/developer-quickstart.md`
  3.1 节复跑通过。
