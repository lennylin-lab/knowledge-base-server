# Session detail messages scroll pagination

## Goal

Add cursor-based pagination to GET /sessions/{session_id} so the Flutter client can scroll-load chat history instead of fetching all messages at once.

## 背景

`GET /api/v1/sessions/{session_id}` 目前一次性返回会话的全部消息（`SessionDetail.messages`，按时间正序）。长会话下响应会越来越大，前端也需要一次性渲染所有消息。本任务为该接口增加基于 keyset 游标的滚动分页。

## Requirements

1. `GET /sessions/{session_id}` 支持 `limit` 和 `cursor` 两个可选 query 参数：
   - **不传参数时行为完全不变**：返回全部消息（向后兼容，前端可渐进迁移）。
   - 传 `limit` 时：返回**最新**的 `limit` 条消息作为首页（页内仍按 `created_at ASC, id ASC` 正序返回，方便前端直接追加渲染），并给出 `next_cursor` 指向更早的消息。
   - 传 `cursor` + `limit` 时：返回该游标（不含）**之前（更早）**的一页消息，正序返回；没有更早消息时 `next_cursor` 为 `null`。
2. 游标沿用会话列表的 keyset 范式：`(created_at, id)` 复合键 + `app/utils/cursor.py` 的 `encode_cursor`/`decode_cursor`，`limit+1` 探测是否还有更早的一页。
3. 分页读取必须保持现有租户隔离与软删除过滤（join live session，`tenant_id` + `deleted_at IS NULL`）。
4. 响应结构：分页时新增一个与 `SessionPage` 对称的消息页 schema（`MessagePage`：`items: list[MessageRead]` + `next_cursor`）；`SessionDetail`（全量路径）保持不变。

## 非目标

- 不在本任务中暴露 `MessageRead.sources`（引用回放是后续任务）。
- 不改任何写路径、不改会话列表分页。
- 不做消息级搜索/过滤。

## Acceptance Criteria

- [ ] 不带参数调用 `GET /sessions/{id}`：响应与改动前完全一致（全部消息、正序）。
- [ ] `limit=N`：返回最新 N 条（正序），`next_cursor` 非空当且仅当存在更早的消息。
- [ ] 用 `next_cursor` 翻页能完整重建全部消息，顺序与全量返回一致，无重复、无遗漏；翻到最早一页后 `next_cursor` 为 `null`。
- [ ] `limit` 校验：`ge=1, le=100`（与会话列表一致）。
- [ ] 无效 cursor：按会话列表现有的错误处理约定处理（遵循 `error-handling` spec）。
- [ ] 其他租户的 session 不可通过任何分页参数读取（扩展现有租户隔离测试）。
- [ ] lint / type-check / 相关 pytest 全绿。
