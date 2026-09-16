# knowledge-base-server 对接 knowledge-base-gateway v1.3 指南（模型控制面）

本文是 Gateway 对接系列的第三篇：[`gateway-integration.md`](./gateway-integration.md)
（v1.1 基础对接）、[`gateway-v1.2-integration.md`](./gateway-v1.2-integration.md)
（chat/tools 全链路）之后的 v1.3 增量——**模型控制面**。v1.3 把 server 仍在
自持的三类模型相关配置统一托管到 Gateway：embedding 调用代理、subject 默认
模型、RAG 检索阈值（retrieval profile）。

升级前提与基础流程（`.env`、key 获取、Compose、错误表）与 v1.1/v1.2 文档
一致，本文只讲增量。

## 1. 升级 Gateway

```bash
cd ../knowledge-base-gateway
git pull
docker compose up -d --build          # Compose 部署：migration job 自动执行
# 手动部署：先迁移再重启
go run ./cmd/migrate up
go run ./cmd/migrate version          # 应显示 version 5 (dirty=false)
```

迁移 0005 全部为加法（`access_policies.default_model` /
`default_embedding_model` 两个可空 FK 列、`model_catalog.retrieval_profile`
JSONB），含 down 回滚；不迁移新 Gateway 起不来，只迁移不升级无副作用。

## 2. Feature 1：embedding 走 Gateway 代理

### 2.1 前置：catalog 声明 embedding 能力

与其他能力一样，`embeddings` 与向量维度 `embedding_dim` 都是 **catalog 目录
属性**（pgvector 列宽固定，换模型/换维度是迁移事件——由 Gateway 声明，
server 读取，不是运行时参数）。给要代理的模型补声明：

```sql
UPDATE model_catalog
SET capabilities = capabilities || jsonb_build_object(
        'embeddings', true,
        'embedding_dim', 1536       -- 必须与 pgvector 列宽一致
    ),
    config_version = config_version + 1
WHERE public_name = '<你的公开模型名>';
```

声明与上游实际输出宽度不一致时，Gateway 会在请求时返回 500
`embedding_dim_mismatch`（宁可失败也不静默返回错宽度向量）。

### 2.2 server 侧配置

`src/app/llm/embeddings.py` 本来就走 `AsyncOpenAI(base_url=...)`，只需把
指向从 provider 换成 Gateway：

```dotenv
# 指向 Gateway（与 KB_CHAT_BASE_URL 同值），SDK 自动追加 /embeddings
KB_EMBEDDING_BASE_URL=http://127.0.0.1:8091/v1
# Gateway key（不再持有上游 provider key）
KB_EMBEDDING_API_KEY=<gateway-api-key>
KB_EMBEDDING_MODEL=<Gateway 公开模型名>
# 维度改为从模型发现读取；env 保留作为离线/测试逃生门
KB_EMBEDDING_DIM=1536
```

启动时（或定期）从模型发现读取维度，env 仅兜底：

```python
detail = client.models.retrieve(settings.EMBEDDING_MODEL)  # GET /v1/models/{model}
dim = detail.capabilities.get("embedding_dim") or settings.EMBEDDING_DIM
```

行为约定：

- embedding 的 token 与 chat **同一个** subject 日/月配额池（usage 已知按
  实际结算，未知保守预扣）；429 语义与 chat 一致（`Retry-After`）；
- 限流/并发/审计/chat 同一套；审计只记 token 与延迟元数据，绝无向量或
  输入内容；
- 回滚开关：`GATEWAY_EMBEDDINGS_ENABLED=false` 单独关闭该端点（404）。

### 2.3 冒烟

```bash
curl -s "$GATEWAY_URL/v1/embeddings" \
  -H "Authorization: Bearer $KB_GATEWAY_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"<公开模型名>","input":"为我讲讲Python"}'
# 期望：{"object":"list","data":[{"object":"embedding","index":0,"embedding":[...]}],"usage":{...},"model":"<公开模型名>"}
```

dev 模式的 fake provider 返回确定性向量（输入哈希派生、宽度恰为声明的
`embedding_dim`），适合断言维度而不适合断言语义。

## 3. Feature 2：模型由 Gateway 指定（subject 默认模型）

`access_policies` 新增 `default_model`（chat/responses）与
`default_embedding_model`（embeddings）两个可空槽位。**请求缺 `model` 字段
时 Gateway 自动回填对应槽位**；显式传 `model` 行为不变（A/B 与逃生用）。
默认值不会绕过授权：回填后的模型仍须在 subject 的 grants 内，否则 403。

设置默认（管理面，原子事务 + 管理审计，热生效）：

```bash
curl -s -X POST -H "Authorization: Bearer $GATEWAY_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  "$GATEWAY_ADMIN_URL/admin/policies/subject_default/default-model" \
  -d '{"model":"<chat-公开模型名>","kind":"chat"}'
# embedding 槽位同接口，kind="embedding"
curl -s -H "Authorization: Bearer $GATEWAY_ADMIN_TOKEN" \
  "$GATEWAY_ADMIN_URL/admin/policies"    # 两个槽位在视图里可见
```

审计写入失败会回滚整个变更（`update_failed`）；提交成功但运行时刷新失败
返回 `refresh_failed`（变更已保存且可审计）。dev 模式用
`GATEWAY_DEFAULT_MODELS="subject:chat-model[:embedding-model]"`。

server 侧：`KB_CHAT_MODEL` 变为**可选**——配置了则显式指定（现行为）；
未配置则请求不带 `model`，由 Gateway 回填。若两个槽位都未配置且请求缺
`model`，返回 400 `invalid_request`。

## 4. Feature 3：RAG 检索阈值托管（retrieval profile）

阈值组挂在 catalog 模型行（`model_catalog.retrieval_profile` JSONB，
**模型级**），随模型发现下发：

```bash
curl -s -H "Authorization: Bearer $KB_GATEWAY_KEY" \
  "$GATEWAY_URL/v1/models/<公开模型名>" | jq .retrieval_profile
# 示例：{"search_vector_max_distance":0.45,"search_rrf_min_relative":0.35,...}
```

- server 侧 `SEARCH_*` env 值保持为**默认兜底**；profile 存在时覆盖对应
  键——Gateway 不可用时 server 仍可独立启动；
- profile 内容对 Gateway 是不透明 JSON（结构校验归 server）；换模型即换
  阈值档，`config_version` 随声明演进并在发现接口可见；
- 本期没有管理端变更接口：profile 属 catalog 数据，由 SQL/迁移工具维护
  （与「migrations 拥有 schema」约定一致）。

## 5. 升级验收清单

- [ ] `migrate version` 显示 `version 5 (dirty=false)`；
- [ ] 目标模型的 capabilities 含 `embeddings`/`embedding_dim`，且维度与
  pgvector 列宽一致；
- [ ] `/v1/embeddings` 冒烟返回正确宽度向量（2.3）；
- [ ] admin 设置 chat/embedding 两个默认槽位后，不带 `model` 的请求审计里
  记录的是回填后的模型；
- [ ] `/v1/models/{model}` 的 `retrieval_profile` 与 catalog 一致，server
  启动日志显示阈值取值来源（profile/兜底）；
- [ ] 人为制造维度不一致（catalog 改 `embedding_dim`）确认 500
  `embedding_dim_mismatch`，改回后恢复。
