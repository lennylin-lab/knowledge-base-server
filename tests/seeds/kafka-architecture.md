---
title: "Kafka 架构原理与顺序、可靠性保障"
tags:
  - kafka
  - mq
  - middleware
---

# Kafka 架构原理与顺序、可靠性保障

## 核心概念

- **Topic**:逻辑消息分类;**Partition**:物理分片,每个分区是一个有序、不可变的追加日志。
- **Producer**:按 key 哈希或轮询选择分区。
- **Consumer Group**:同组内每个分区只被一个消费者消费,实现负载均衡;不同组各自独立消费。
- **Broker**:服务节点;**Controller**(KRaft 模式下由 Raft 仲裁)负责分区 leader 选举。
- 副本机制:每个分区有 1 个 leader + N 个 follower,ISR(In-Sync Replicas)是保持同步的副本集合。

```text
Topic: order-events (3 partitions)

P0: [0][1][2][3][4]  ← leader on broker-1
P1: [0][1][2][3]     ← leader on broker-2
P2: [0][1][2][3][4][5] ← leader on broker-3

Group "billing":   c1 → P0, c2 → P1+P2
Group "analytics": c1 → P0+P1+P2
```

## 为什么快

1. 顺序写磁盘(追加日志)+ 操作系统 page cache,避免随机 IO。
2. 零拷贝(`sendfile`)传输数据到消费者。
3. 批量发送 + 压缩(lz4/zstd/snappy)摊薄网络与 IO 成本。

## 顺序性保障

Kafka 只保证**单分区内有序**。需要全局按业务键有序时,把同一 key(如订单 ID)的消息发到同一分区。生产端 `max.in.flight.requests.per.connection > 1` 且开启重试时,需设置 `enable.idempotence=true`(幂等生产者,基于 PID + 序列号去重),否则重试会导致乱序。

## 可靠性语义

| 配置 | 说明 |
| --- | --- |
| `acks=0` | 不等确认,可能丢 |
| `acks=1` | leader 落盘即确认,leader 切换可能丢 |
| `acks=all` + `min.insync.replicas=2` | ISR 内多数落盘,不丢的推荐配置 |

消费端把自动提交(`enable.auto.commit=false`)改为业务处理成功后手动提交,配合幂等消费(唯一键去重表)实现**至少一次 + 幂等 = 精确一次效果**。

## Rebalance 与消费者管理

分区分配策略:Range、RoundRobin、Sticky、CooperativeSticky(增量重平衡,推荐)。Rebalance 触发条件:成员增减、订阅变化、分区数变化。

频繁 Rebalance 的常见原因:消费耗时超过 `max.poll.interval.ms` 导致心跳正常但被踢出。对策:下调单次 `max.poll.records`、异步处理 + 回压控制。

## 消费位移与积压处理

```bash
# 查看积压
kafka-consumer-groups.sh --bootstrap-server localhost:9092 \
  --group billing --describe

# 从最早/指定时间重置位移
kafka-consumer-groups.sh --group billing --topic order-events \
  --reset-offsets --to-earliest --execute
```

积压扩容的硬限制:消费者数 ≤ 分区数。预估吞吐时要提前规划分区数(分区也增加端到端延迟与故障恢复成本,不是越多越好)。

## 与其它 MQ 的取舍

- **RocketMQ**:Java 技术栈友好,事务消息、延迟消息原生支持。
- **RabbitMQ**:路由灵活(Exchange 模型),适合低延迟任务分发。
- **Kafka**:高吞吐日志流、事件溯源、流处理(Kafka Streams / Flink 上游)的事实标准。
