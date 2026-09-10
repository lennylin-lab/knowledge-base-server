---
title: "MySQL 索引、事务与性能优化实践"
tags:
  - mysql
  - database
  - backend
---

# MySQL 索引、事务与性能优化实践

## 存储引擎与索引结构

InnoDB 基于 **B+ 树** 组织索引:非叶子节点只存键,叶子节点存数据并通过双向链表连接,适合范围查询。

- **聚簇索引(主键索引)**:叶子节点存放整行数据,表本身就是按主键组织的。
- **二级索引(辅助索引)**:叶子节点存主键值,查询非索引列需要**回表**。
- **覆盖索引**:查询列全部包含在索引中,免回表,`EXPLAIN` 中表现为 `Using index`。

```sql
-- 联合索引 (a, b, c):最左前缀原则
CREATE INDEX idx_abc ON t (a, b, c);
-- 能命中:a / a,b / a,b,c / a 范围查询后 b 失效
-- 无法命中:b / c / b,c
```

索引失效的常见写法:对索引列使用函数或运算、隐式类型转换(字符串列传数字)、前导模糊 `LIKE '%x'`、`OR` 连接非索引列。

## 事务与 MVCC

InnoDB 事务遵循 ACID,隔离级别:

| 隔离级别 | 脏读 | 不可重复读 | 幻读 |
| --- | --- | --- | --- |
| READ UNCOMMITTED | 可能 | 可能 | 可能 |
| READ COMMITTED | 避免 | 可能 | 可能 |
| REPEATABLE DEFAULT(InnoDB 默认) | 避免 | 避免 | 大程度避免 |
| SERIALIZABLE | 避免 | 避免 | 避免 |

**MVCC 实现**:每行记录隐藏 `trx_id`(最后修改事务)与 `roll_pointer`(指向 undo log 版本链)。读操作根据 Read View(活跃事务快照)沿版本链找到对当前事务可见的版本。RC 级别每条语句生成一次 Read View,RR 级别整个事务复用第一次的 Read View。

**当前读与快照读**:`SELECT ... FOR UPDATE` / `UPDATE` 是当前读(读最新版本并加锁),普通 SELECT 是快照读。RR 下的幻读通过 MVCC(快照读)+ 间隙锁 Next-Key Lock(当前读)共同抑制。

## 锁

- **行锁**基于索引实现:更新条件无索引可用时会升级为锁全表(所有行)。
- **间隙锁(Gap Lock)** 锁索引区间,防止插入;**Next-Key Lock** = 记录锁 + 间隙锁。
- 死锁:`SHOW ENGINE INNODB STATUS` 查看 LATEST DETECTED DEADLOCK;固定加锁顺序、小事务、合理索引是预防手段。

## 性能优化清单

```sql
EXPLAIN SELECT * FROM orders WHERE user_id = 42 ORDER BY created_at DESC LIMIT 20;
```

重点看 `type`(至少 range,避免 ALL)、`key`、`rows`、`Extra`(`Using filesort`/`Using temporary` 需要优化)。

1. 深分页:用游标 `WHERE id > last_id LIMIT 20` 替代 `LIMIT 100000, 20`。
2. 大表加字段用 `ALGORITHM=INSTANT`(8.0);索引变更用 `ALGORITHM=INPLACE, LOCK=NONE`。
3. 计数优先用近似值 `EXPLAIN` 或维护计数表,避免 `COUNT(*)` 全扫。
4. 8.0 特性:窗口函数、CTE、降序索引、函数索引、`invisible index` 用于安全下线索引。
5. 连接池必配(如 HikariCP),避免短连接风暴;`max_connections` 结合内存规划。
