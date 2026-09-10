---
title: "Redis 数据结构、缓存模式与高可用"
tags:
  - redis
  - cache
  - middleware
---

# Redis 数据结构、缓存模式与高可用

## 核心数据结构与典型用途

| 类型 | 底层编码 | 典型场景 |
| --- | --- | --- |
| String(int/embstr/raw) | SDS 动态字符串 | 缓存、计数器、分布式锁 |
| Hash(listpack/hashtable) | 哈希表 | 对象属性部分读写 |
| List(quicklist = 链表 + listpack) | 快表 | 消息队列、时间线 |
| Set(intset/listpack/hashtable) | 哈希表 | 去重、共同关注(SINTER) |
| ZSet(listpack/skiplist) | 跳表 + 哈希 | 排行榜、延迟队列 |
| Stream(Radix Tree) | 基数树 | 可持久化消息队列、消费组 |
| Bitmap / HyperLogLog / GEO | 位图/基数估算 | 签到、UV 统计、附近的人 |

```bash
# 排行榜:ZSet
ZADD leaderboard 9800 "alice"
ZINCRBY leaderboard 100 "alice"
ZREVRANGE leaderboard 0 9 WITHSCORES

# 分布式锁(推荐 Redisson / redlock 实现,SET NX 只是最简形态)
SET lock:order:42 <token> NX PX 30000
# 释放需 Lua 保证「判断持有者 + 删除」原子性
```

## 过期与淘汰策略

- 过期删除:**惰性删除**(访问时检查)+ **定期抽样删除**。
- 内存淘汰(`maxmemory-policy`):`noeviction`(默认)、`allkeys-lru`、`volatile-lru`、`allkeys-lfu`、`volatile-ttl` 等。缓存场景常用 `allkeys-lru` 或 `allkeys-lfu`。

## 缓存三大问题

1. **缓存穿透**:查询不存在的数据绕过缓存打库。对策:缓存空值(短 TTL)、布隆过滤器前置拦截。
2. **缓存击穿**:热点 key 过期瞬间大量请求打库。对策:互斥锁重建、逻辑过期(物理永不过期 + 异步刷新)。
3. **缓存雪崩**:大批 key 同时过期或 Redis 宕机。对策:TTL 加随机抖动、多级缓存、集群高可用与限流降级。

## 缓存一致性

先更新数据库再删除缓存(Cache Aside)是最常用组合,配合以下增强:

- **延迟双删**:写后删一次,延迟几百毫秒再删一次,覆盖读写并发窗口。
- **订阅 binlog 异步删除**(Canal → MQ → 删除),把一致性做成最终一致。
- 强一致需求不要依赖缓存,直接读库或加分布式锁。

## 持久化

- **RDB**:定时 fork 子进程全量快照,恢复快,可能丢最近数据。
- **AOF**:追加写命令,`appendfsync everysec` 兼顾性能与安全(最多丢 1 秒);AOF 重写压缩体积。
- 混合持久化(4.0+):RDB 头 + 增量 AOF,重启恢复快且丢失少,推荐开启。

## 高可用架构

- **主从复制**:异步复制,从库重放 RDB + 命令流;`min-replicas-to-write` 可降低脑裂丢写。
- **哨兵(Sentinel)**:监控 + 自动故障转移 + 通知,客户端需支持哨兵发现主库。
- **Cluster**:16384 个 slot 分片,节点 Gossip 协议通信;key 用 hash tag `{user1}:orders` 保证多 key 同槽;集群模式下 mget/事务/lua 只能操作同槽 key。

## 实践红线

1. 大 key(单 value > 10KB 或集合元素过多)与热 key 要拆分或本地缓存兜底;删除大集合用 `UNLINK` + `SCAN` 分批。
2. 生产禁用 `KEYS *`、`FLUSHALL`;用 `SCAN` 渐进遍历。
3. Lua 脚本保持短小,Redis 执行脚本期间阻塞其它命令。
4. Pipeline 批量操作能大幅降低 RTT,但注意单命令大小与超时。
