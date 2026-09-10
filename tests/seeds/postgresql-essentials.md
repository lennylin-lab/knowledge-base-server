---
title: "PostgreSQL 特性精要与 MySQL 的差异对比"
tags:
  - postgresql
  - database
  - backend
---

# PostgreSQL 特性精要与 MySQL 的差异对比

## 为什么选 PostgreSQL

PostgreSQL 是最先进的开源关系数据库,定位「一件事做对」:严格的 SQL 标准兼容、丰富的类型系统、可扩展框架(扩展插件近乎无限制)。pgvector、PostGIS、TimescaleDB 等扩展让它一个库覆盖向量检索、地理信息、时序场景。

## 类型系统亮点

```sql
-- 数组类型
SELECT * FROM users WHERE tags @> ARRAY['admin'];

-- JSONB:二进制存储 + GIN 索引,可高效查询
CREATE INDEX idx_meta ON events USING gin (payload);
SELECT payload->>'order_id' FROM events WHERE payload @> '{"status": "paid"}';

-- 范围类型
SELECT * FROM bookings WHERE during && tsrange('2026-09-01', '2026-09-10');

-- 自定义复合类型、枚举、uuid、网络地址类型等
```

## 索引类型

与 MySQL 只有 B+ 树不同,PG 提供多种索引:

- **B-tree**:默认,等值与范围。
- **Hash**:仅等值,8.0 后 WAL 记录,更小更快。
- **GIN**:倒排,JSONB、数组、全文检索必备。
- **GiST**:地理、范围、KNN。
- **BRIN**:块级范围索引,超大时序表性价比极高。
- **部分索引**:`CREATE INDEX ... WHERE status = 'active'`,只索引热数据。

## MVCC 与 VACUUM

PG 的 MVCC 通过在堆中保留多版本元组实现(不像 InnoDB 写 undo log):UPDATE = 插入新版本 + 旧版本标记失效。死元组由 **VACUUM** 回收,autovacuum 负责自动清理。

关键监控:

```sql
-- 死元组比例过高说明 autovacuum 跟不上
SELECT relname, n_dead_tup, n_live_tup,
       n_dead_tup::float / greatest(n_live_tup, 1) AS dead_ratio
FROM pg_stat_user_tables
ORDER BY dead_ratio DESC;
```

长事务会阻止 VACUUM 推进,导致表膨胀——务必避免事务中挂起交互(如事务内调用外部 HTTP)。

## 与 MySQL 的主要差异

| 维度 | PostgreSQL | MySQL(InnoDB) |
| --- | --- | --- |
| MVCC | 堆内多版本 + VACUUM | undo log 版本链 |
| 复制 | 逻辑复制、物理流复制,延迟极低 | binlog 异步/半同步 |
| 并发控制 | 全版本可读,写不阻塞读 | 同样写不阻塞读,实现路径不同 |
| 查询优化器 | 基于代价,支持并行查询、Hash Join | 8.0 仍有 Hash Join 限制 |
| 全文检索 | 内置 tsvector/tsquery | 依赖 FULLTEXT 或外部 ES |
| DDL | 大部分可事务回滚 | DDL 隐式提交 |
| 进程/线程模型 | 每连接一个进程 | 单进程多线程 |

## 实用技巧

```sql
-- UPSERT
INSERT INTO metrics (key, value) VALUES ('qps', 100)
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;

-- RETURNING 拿回写入结果,省一次 SELECT
INSERT INTO orders (user_id) VALUES (42) RETURNING id, created_at;

-- CTE + 窗口函数
WITH ranked AS (
  SELECT user_id, amount,
         ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY amount DESC) AS rn
  FROM payments
)
SELECT * FROM ranked WHERE rn <= 3;
```

连接层务必使用 PgBouncer 等 pooler(注意 transaction pooling 模式下 prepared statement 的兼容性);生产参数重点关注 `shared_buffers`、`work_mem`、`effective_cache_size`、`max_wal_size`。
