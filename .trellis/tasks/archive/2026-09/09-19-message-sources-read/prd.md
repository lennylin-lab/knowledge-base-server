# Expose ChatMessage.sources in message read API

## Goal

Add sources to `MessageRead` so session history replay includes the `[n] → documentId` citation mapping stored per assistant message (`ChatMessage.sources`, `src/app/models/chat.py:111`). This fixes the "reopening a session loses citation links" gap: the DB already persists each run's hits in retrieval order, but the session detail / paginated message APIs drop the column.

## Requirements

1. 新增一个**瘦身的引用条目 schema**（如 `SourceRef`），字段：`document_id: UUID`、`document_title: str`、`document_tags: list[str]`、`chunk_index: int`。列表顺序即引用编号（第 1 个元素 = `[1]`），不新增显式 index 字段。
2. **不暴露完整存储内容**：`ChatMessage.sources` 存的是 `SearchHit.model_dump()`，含 `content`（整块正文）、`score`、`es_rank`、`vector_distance`、`es_score`。历史回放只需要跳转映射，把整块正文和内部相关性信号塞进会话详情响应会显著膨胀 payload 并泄漏检索内部信息；因此 `MessageRead.sources` 的类型为 `list[SourceRef] | None`，由服务层从存储 dict 中投影出 `SourceRef`。
3. `MessageRead` 增加 `sources: list[SourceRef] | None = None`。全量（`SessionDetail`）和分页（`MessagePage`）两条读路径都要带出。
4. 投影要宽容：存储 dict 缺字段/多余字段不应导致 500（数据由本系统写入，正常齐全；投影按已知键取值即可）。
5. 用户消息 `sources` 恒为 NULL —— 响应中就是 `null` 或省略，前端只对 assistant 消息做映射。

## 非目标

- 不改写路径（写入侧不变）。
- 不改 `sources` SSE 事件结构。
- 不做前端改动（Flutter 侧对接由 issue lennylin-lab/knowledge-base-flutter#6 跟踪）。

## Acceptance Criteria

- [ ] `GET /sessions/{id}`（全量）返回的 assistant 消息带 `sources`，元素顺序与写入时检索顺序一致（即 `[1..N]` 编号），字段为 `SourceRef` 四字段。
- [ ] `GET /sessions/{id}?limit=...`（分页路径）同样带出 `sources`。
- [ ] assistant 消息 `sources` 为 NULL（无检索/失败）时响应为 `null`；用户消息恒为 `null`。
- [ ] 响应中不出现 `content` / `score` / `es_rank` / `vector_distance` / `es_score`。
- [ ] 现有会话 API 测试全绿，新增测试覆盖上述四条；lint / type-check 全绿。
