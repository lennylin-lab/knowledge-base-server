# knowledge-base-server 与 knowledge-base-gateway 联调指南

本文说明如何让 `knowledge-base-server` 通过 OpenAI-compatible API 调用
`knowledge-base-gateway`。

Gateway 进程和 `migrate` 命令都会尝试从当前工作目录加载可选的 `.env`。
已有的进程环境变量优先于文件值；`.env` 缺失是允许的，但文件不可读或格式
错误会使命令启动失败。两端的 `.env` 是分开的，不能把 Gateway 根目录的文件
当成 server 的配置文件。Gateway 仓库中的 `.env` 和本地
`docker-compose.yml` 已被 Git 忽略，但包含密钥的文件仍应限制权限，不能提交或
放入镜像；生产环境优先使用编排平台的环境变量/secret 注入。

> 重要兼容性说明：Gateway 当前支持的是最小 chat completions 子集，不支持
> OpenAI `tools` / function calling 或 MCP 字段。`knowledge-base-server` 的
> QA agent 默认需要调用 `search_knowledge` 工具来检索知识库。因此本文中的
> Gateway 冒烟测试可以证明网络、鉴权、模型和普通对话链路；它不能证明带工具
> 调用的知识库问答已经可用。生产 RAG 接入前，需要先为 Gateway 增加工具调用
> 透传能力，或者暂时让 `KB_CHAT_*` 直连一个支持工具调用的模型服务。

## 1. 架构和职责

```text
业务客户端
    |
    |  POST /api/v1/chat (knowledge-base-server 自有 SSE 协议)
    v
knowledge-base-server
    |
    |  OpenAI-compatible client, KB_CHAT_*
    |  POST /v1/chat/completions
    v
knowledge-base-gateway
    |
    |  provider route / retry / timeout
    v
OpenAI / Anthropic / 其他兼容上游
```

| 组件 | 负责内容 |
| --- | --- |
| `knowledge-base-server` | 文档检索、会话、RAG prompt、`/api/v1/chat` SSE 事件、embedding 配置 |
| `knowledge-base-gateway` | Gateway API key、公开模型名、路由、限流、并发、每日/月度 token quota、上游重试和审计 |
| Gateway PostgreSQL/Redis | Gateway 的 catalog、key、policy、audit 和分布式限流/quota |
| server PostgreSQL/Redis | server 自己的文档/会话数据、索引队列和缓存 |

两套 PostgreSQL/Redis 配置相互独立，不要把 `KB_*` 地址填到
`GATEWAY_*`，也不要把上游 provider secret 配置到 server。

## 2. 前置条件

- Gateway：Go、PostgreSQL、Redis（生产/Compose 模式）。
- server：Python 3.12、`uv`、自身的 PostgreSQL、Elasticsearch；安装和迁移步骤见 server 根目录 `README.md`。
- 两个进程必须能通过网络访问对方。宿主机进程访问 Compose Gateway 使用 `127.0.0.1:8091`；容器访问时不能使用容器自身的 `127.0.0.1`，应使用共享网络中的服务名（例如 `gateway:8080`）。
- 使用 `.env` 时分别在 Gateway 和 server 仓库根目录执行命令；相对路径按进程的当前工作目录解析。也可以完全不用文件，直接注入进程环境变量。

## 3. 启动 Gateway

### 3.1 本地 fake provider（最快的连接测试）

在 Gateway 仓库根目录执行：

```bash
cp .env.example .env
go run ./cmd/gateway
```

`.env` 中至少配置：

```dotenv
GATEWAY_API_KEYS=kb-local:kb-server:sk-kb-local
GATEWAY_MODELS=gateway-echo:fake:echo-model
GATEWAY_PROVIDER=fake
GATEWAY_ADDR=:8080
```

本模式使用进程内 key、model 和 limiter，不需要 Gateway PostgreSQL/Redis。也可以
用 `export GATEWAY_...=...` 代替 `.env`；同名进程环境变量会覆盖文件值。
`GATEWAY_API_KEYS` 格式是 `id:subject:plaintext-key`，只用于开发。Gateway
不会持久化或打印 provider secret；若 secret 写在本地 `.env`，它仍然存在于磁盘，
请按本地凭据保护，生产改用运行时环境变量或 secret 存储。

此时 Gateway 地址为 `http://127.0.0.1:8080`，公开模型名为
`gateway-echo`，fake provider 会把最后一条 user 消息返回为 `echo: ...`。

### 3.2 Docker Compose（数据库模式）

在 Gateway 仓库执行：

```bash
cp docker-compose.yml.example docker-compose.yml
docker compose up -d --build
docker compose ps
curl -fsS http://127.0.0.1:8091/healthz
curl -fsS http://127.0.0.1:8091/readyz
```

`docker-compose.yml.example` 是版本控制中的模板，复制后的
`docker-compose.yml` 才是本地 Compose 入口。Compose 会启动 PostgreSQL、Redis、
一次性 migration job 和 Gateway：

- Gateway chat 地址：`http://127.0.0.1:8091`
- Gateway admin 地址：`http://127.0.0.1:8092`
- 内部 Gateway 端口：`8080`
- 默认 seeded 公开模型：`gateway-echo`
- 默认 admin token：`smoke-admin-throwaway`，只适合本地临时环境，生产必须覆盖 `GATEWAY_ADMIN_TOKEN`

Compose 变量插值通常按 shell 环境变量、项目根目录 `.env`、模板中的
`${...:-default}` 依次取值；这与容器内 Go 进程的环境加载是两层机制。根目录的
`.env` 只用于 `${...}` 插值；只有各 service 的
`environment:` 段明确列出的变量才会传进容器。把 `OPENAI_API_KEY` 或
`ANTHROPIC_API_KEY` 放进 `.env` 不会自动注入 Gateway；如果数据库中的 enabled
provider 是对应类型，必须在部署的 Gateway service 中显式注入该 secret（或使用
容器编排平台的 secret 机制）。同理，Compose 的 `.env` 不会自动出现在当前 shell
里，调用 admin API 时请在 shell 中设置相同的 `GATEWAY_ADMIN_TOKEN`。

完整启动和依赖故障恢复检查：

```bash
scripts/smoke.sh --skip-outage
```

`scripts/smoke.sh --down` 会连同 PostgreSQL volume 一起删除，只在可丢弃的本地
环境使用。

### 3.3 获取数据库模式的 Gateway key

数据库模式不读取开发用的 `GATEWAY_API_KEYS` / `GATEWAY_MODELS`；key 通过 admin
API 创建，明文只返回一次：

```bash
export GATEWAY_URL=http://127.0.0.1:8091
export GATEWAY_ADMIN_URL=http://127.0.0.1:8092
export GATEWAY_ADMIN_TOKEN=smoke-admin-throwaway

export KB_GATEWAY_KEY="$(
  curl -fsS -X POST "$GATEWAY_ADMIN_URL/admin/keys" \
    -H "Authorization: Bearer $GATEWAY_ADMIN_TOKEN" \
    -H 'Content-Type: application/json' \
    -d '{"subject":"subject_default","expires_in_hours":24}' | jq -r '.key'
)"

test -n "$KB_GATEWAY_KEY"
```

如果没有 `jq`，请手工从响应中读取 `key`，不要把明文 key 写入仓库或日志。
需要轮换或撤销时使用 `POST /admin/keys/{id}/rotate` 和
`POST /admin/keys/{id}/revoke`。

### 3.4 手动执行 Gateway migration

在 Gateway 仓库根目录运行时，`migrate` 会从当前目录的 `.env` 读取
`GATEWAY_DATABASE_URL`：

```bash
go run ./cmd/migrate up
go run ./cmd/migrate version
go run ./cmd/migrate steps -1
```

也可以用 `-dsn <postgres-dsn>` 显式指定 DSN；显式参数优先于 `.env` 和进程环境。
Compose 的一次性 migration job 已经使用内部网络 DSN 执行过 `up` 时，不需要再手动
执行上述命令。

## 4. 配置 knowledge-base-server

在 server 仓库根目录复制 `.env.example` 为 `.env`，只替换 chat 相关变量。server
自身也按当前工作目录加载 `.env`，进程环境变量优先；不要依赖 Gateway 目录下的
`.env` 被 server 读取：

```dotenv
# Gateway 的 OpenAI-compatible API 根地址，必须停在 /v1
KB_CHAT_BASE_URL=http://127.0.0.1:8091/v1
# 这是 Gateway key，不是 OpenAI/Anthropic 上游 key
KB_CHAT_API_KEY=<gateway-api-key>
# Gateway 的公开模型名，不是上游模型名
KB_CHAT_MODEL=gateway-echo
```

本地 `go run` 模式把 `KB_CHAT_BASE_URL` 改成
`http://127.0.0.1:8080/v1`。不要写成
`.../v1/chat/completions`，OpenAI SDK 会自动追加该路径。

embedding 与 chat 独立配置。Gateway 当前只有 chat completions，不提供
embedding endpoint，因此 embedding 应继续指向 server 可访问的独立 provider：

```dotenv
KB_EMBEDDING_BASE_URL=https://api.openai.com/v1
KB_EMBEDDING_API_KEY=<embedding-provider-key>
KB_EMBEDDING_MODEL=text-embedding-3-small
```

`KB_REDIS_URL` 是 server 的 ARQ/cache Redis；Gateway 使用
`GATEWAY_REDIS_ADDR`。两者可以是同一 Redis 服务，但用途、前缀和生命周期不同，
不要因此省略 Gateway 的 readiness 检查。

## 5. 先验证 Gateway 本身

### 5.1 非流式请求

```bash
curl -i -sS "$GATEWAY_URL/v1/chat/completions" \
  -H "Authorization: Bearer $KB_GATEWAY_KEY" \
  -H 'Content-Type: application/json' \
  -H 'X-Request-ID: kb-gw-smoke-001' \
  -H 'X-Trace-ID: kb-gw-trace-001' \
  -d '{
    "model": "gateway-echo",
    "messages": [{"role": "user", "content": "gateway smoke"}],
    "max_tokens": 32,
    "stream": false
  }'
```

成功响应是 OpenAI-compatible `chat.completion` JSON，fake provider 的内容应为
`echo: gateway smoke`。响应会回显 `X-Request-ID` 和 `X-Trace-ID`。

### 5.2 流式请求

```bash
curl -N -sS "$GATEWAY_URL/v1/chat/completions" \
  -H "Authorization: Bearer $KB_GATEWAY_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "gateway-echo",
    "messages": [{"role": "user", "content": "stream smoke"}],
    "max_tokens": 32,
    "stream": true
  }'
```

响应为 `text/event-stream`，每个数据块形如 `data: {...}`，成功结束标记为
`data: [DONE]`。一旦已经收到数据块，客户端不要重新发送同一个流式请求；此时
失败只能表现为截断流或终止 SSE 错误事件。

## 6. 启动并验证 knowledge-base-server

完成 server 自身的数据库、Elasticsearch 和 migration 准备后：

```bash
cd ../knowledge-base-server
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --port 8000
```

server 的公开 chat API 与 Gateway API 不是同一个协议。调用 server：

```bash
curl -N -sS http://127.0.0.1:8000/api/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"question":"请总结最近的文档","limit":8}'
```

server 返回自己的 SSE 事件序列，常见事件包括：

- `run_started`：包含 `run_id`、检索模式和可选 `session_id`；
- `sources`：检索结果；
- `status`：例如 `rewriting_query`、`generating`；
- `answer_delta`：答案片段；
- `done`：成功结束；
- `error`：流开始后发生失败时的终止事件。

后续会话可把 `run_started` 或 `done` 中的 `session_id` 传回：

```json
{
  "question": "那它的限制是什么？",
  "limit": 8,
  "session_id": "<previous-session-uuid>"
}
```

Gateway 的 `data: {...}` 块是 server 内部的 provider 流，业务客户端只应解析
server 的事件名，不能把两种 SSE 协议混用。

使用 3.1 的 fake provider 做这一步时，最重要的验收项是 server 能建立 provider
连接并完成一次普通回答；由于当前 Gateway 不会转发工具定义，响应可能没有
`sources`，这不代表知识库检索已经成功。只有 Gateway 支持工具调用后，才应把
`sources`、引用和检索质量纳入端到端验收。

## 7. Gateway 请求约定

Gateway 接受的最小请求字段：

```json
{
  "model": "<Gateway public model>",
  "messages": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "..."}
  ],
  "temperature": 0.2,
  "max_tokens": 1024,
  "stream": false
}
```

请求限制默认是 1 MiB body、最多 64 条消息、每条消息最多 32,000 个字符。
`max_tokens` 还会受到 Gateway access policy 的 `max_output_tokens` 限制。
`model` 必须是 Gateway catalog 中允许该 subject 使用的公开名；不能填
`gpt-4o-mini` 等上游名，除非它本身就是 catalog 的公开名。

## 8. 错误和重试

Gateway 非流式错误使用稳定 envelope：

```json
{
  "error": {
    "type": "authentication_error",
    "code": "invalid_api_key",
    "message": "invalid API key",
    "request_id": "req_..."
  }
}
```

| HTTP | 典型 code | 处理建议 |
| --- | --- | --- |
| 400 | `invalid_request`, `upstream_rejected_request` | 修正请求体或模型参数 |
| 401 | `invalid_api_key`, `api_key_expired`, `api_key_revoked` | 轮换 Gateway key；不要重试原请求 |
| 403 | `model_not_allowed` | 检查 `KB_CHAT_MODEL` 和 subject policy |
| 429 | `rate_limit_exceeded`, `quota_exceeded` | 按 `Retry-After` 退避；quota 在下一个 UTC 日/月周期恢复 |
| 503 | `upstream_unavailable`, `upstream_rate_limited`, `no_route_available`, `limiter_unavailable` | 网络/上游恢复后退避重试；`limiter_unavailable` 不代表 quota 被耗尽 |
| 504 | `upstream_timeout` | 可按退避策略重试 |

server 自己会把 provider 错误映射为自己的 envelope（例如
`llm_provider_error`、`rate_limited`）；server SSE 开始后 HTTP 状态已经是 200，
此时只读取最后的 `error` 事件。

server 的 OpenAI client 当前 timeout 为 60 秒、SDK retry 为 2 次；Gateway 默认也有
60 秒总超时和有限重试。部署时不要再叠加一个无上限的代理重试循环，否则一次
用户请求可能产生多层重复调用。只对网络错误、429、503、504 重试，遵守
`Retry-After`，并且在流式响应收到首个 chunk 后停止重试。

## 9. 请求追踪

直接调用 Gateway 时，建议每次生成并保存唯一的 `X-Request-ID`，并在同一 trace
中发送 `X-Trace-ID`。Gateway 会在响应头和错误 envelope 中返回 request id，审计
记录、指标和日志也使用它们。

当前 server 的 `RequestIdMiddleware` 会生成自己的 `X-Request-ID`，但
`get_chat_model()` 尚未把这个 request/trace id 动态注入 OpenAI client 请求头。
因此一次 `/api/v1/chat` 可能同时出现 server request id 和 Gateway request id，
两者暂时不能自动一一对应。排查问题时请同时记录 server 响应头、Gateway 错误
中的 `request_id` 以及时间窗口；生产环境建议后续增加 request-scoped header
转发。

## 10. 当前限制和排障清单

### 工具调用 / MCP

Gateway 的 `chatRequest` 只处理 `model`、`messages`、`temperature`、`max_tokens`
和 `stream` 等最小字段，不会透传 `tools`、`tool_choice`、`function_call` 等字段。
server QA agent 的 `search_knowledge` 和外部 MCP 工具因此不能依赖当前 Gateway
完成 RAG。看到答案没有 `sources`、模型没有触发检索工具时，先确认这是协议
能力限制，不要只重复检查 API key。

### 常见问题

- `curl` 连接失败：确认 Gateway 监听端口；宿主机使用 `8091`（Compose）或 `8080`（`go run`），容器之间使用共享网络服务名。
- `/healthz` 为 200 但 `/readyz` 为 503：检查 Gateway PostgreSQL、Redis、迁移 job 和数据库中的 enabled provider secret。
- 401：`KB_CHAT_API_KEY` 必须是 Gateway key，不是 `OPENAI_API_KEY` 或 `ANTHROPIC_API_KEY`。
- 403：`KB_CHAT_MODEL` 必须是公开 catalog 名，且 key 对应 subject 有 grant；Compose 默认是 `gateway-echo`。
- 503 `chat_unavailable`：server 自己没有读取到 `KB_CHAT_API_KEY`，检查 `.env` 前缀和进程启动目录。
- 502/429：先直接执行第 5 节 Gateway 请求，区分是 Gateway/上游问题还是 server agent 层问题。
- server 有 embedding 错误：Gateway 不提供 embedding，检查 `KB_EMBEDDING_*`，不要把它们替换成 `KB_CHAT_*`。
- server Redis 与 Gateway Redis 混淆：server 使用 `KB_REDIS_URL`，Gateway 使用 `GATEWAY_REDIS_ADDR`；两端可共用实例，但必须分别验证 key 前缀、权限和可用性。

Gateway 的完整客户端 contract 见 Gateway 仓库的
`docs/gateway-client-contract.md`；server 的基础启动、数据库和 Elasticsearch
要求见 server 根目录 `README.md`。

## 11. 业务身份 / 租户 RBAC 与 Gateway 的边界

server 侧的业务资源（documents/sessions/operations）授权与 Gateway 的模型
授权相互独立：

- Gateway key（`KB_CHAT_API_KEY`）只用于 server 调用 Gateway 的模型 API，
  不是用户身份；它不能访问 server 的业务资源路由。
- 用户身份与租户 RBAC（OIDC/Keycloak 兼容验证、`tenant_memberships` 角色
  矩阵、401/403/404 语义）见 `docs/identity-tenants.md`。启用
  `KB_OIDC_ISSUER` 后，业务客户端调用 `/api/v1/*` 需要 `Authorization:
  Bearer <user access token>`；`/api/v1/chat` 的 SSE 协议不变。
- 资源角色永远不会隐含模型权限；Gateway 的 model policy（允许的公开模型、
  quota、限流）只在 Gateway 侧配置，与本文第 5/8 节的验证方式一致，且与
  RBAC 改动无关（opt-in `live_gateway` 探针独立于默认测试套件）。
