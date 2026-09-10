---
title: "Java 核心知识点梳理:JVM、集合与并发"
tags:
  - java
  - jvm
  - concurrency
  - backend
---

# Java 核心知识点梳理

Java 是企业级后端的主流语言,本文梳理 JVM 内存模型、集合框架与并发编程三个最核心的主题。

## JVM 内存区域

JVM 运行时数据区分为以下几块:

- **堆(Heap)**:对象实例的主要分配区域,GC 的主战场,通过 `-Xms`/`-Xmx` 控制初始与最大大小。
- **虚拟机栈(Stack)**:线程私有,存放栈帧(局部变量表、操作数栈、方法出口),`-Xss` 控制栈大小。
- **方法区(Metaspace)**:JDK 8 后由永久代替换为元空间,使用本地内存,存储类元信息。
- **程序计数器**:线程私有,记录当前线程执行的字节码行号。
- **本地方法栈**:为 Native 方法服务。

## 垃圾回收

### 判定对象存活

- 引用计数法(循环引用问题,JVM 不采用)
- 可达性分析:从 GC Roots(虚拟机栈引用、静态变量、常量、JNI 引用)出发,不可达即可回收。

### 经典收集器

| 收集器 | 特点 | 适用场景 |
| --- | --- | --- |
| Serial | 单线程,Stop-The-World | 客户端小内存 |
| Parallel Scavenge | 吞吐量优先 | 后台批处理 |
| CMS | 并发标记清除,低停顿 | 已在 JDK 14 移除 |
| G1 | Region 化分代,可预测停顿 | 大堆通用首选 |
| ZGC | 着色指针,亚毫秒停顿 | 超大堆低延迟 |

```java
// G1 常用参数
// -XX:+UseG1GC -Xms4g -Xmx4g -XX:MaxGCPauseMillis=200
```

## 集合框架

```text
Collection
├── List: ArrayList(数组,随机访问 O(1))、LinkedList(双向链表)、CopyOnWriteArrayList
├── Set: HashSet(哈希)、TreeSet(红黑树,有序)、LinkedHashSet
└── Queue: ArrayDeque、PriorityQueue、BlockingQueue 家族
Map(单独继承体系)
├── HashMap: 数组 + 链表/红黑树,负载因子 0.75,树化阈值 8
├── ConcurrentHashMap: JDK 8 后 CAS + synchronized 锁桶头
└── TreeMap: 红黑树,支持范围查询
```

**HashMap 扩容**:容量翻倍,JDK 8 利用 `hash & oldCap` 判断节点留在原位还是迁移到 `原位置 + oldCap`,避免逐个重新哈希。

## 并发编程

### 线程状态与创建

Java 线程有 6 种状态:NEW、RUNNABLE、BLOCKED、WAITING、TIMED_WAITING、TERMINATED。生产代码推荐使用线程池而不是直接 `new Thread`。

```java
ExecutorService pool = new ThreadPoolExecutor(
    8, 16, 60, TimeUnit.SECONDS,
    new ArrayBlockingQueue<>(1024),
    new ThreadPoolExecutor.CallerRunsPolicy()
);
```

线程池参数经验:CPU 密集型任务核心数取 `N + 1`,IO 密集型取 `2N` 或按等待/计算比估算。**队列用有界队列**,配合合理的拒绝策略,避免 OOM。

### synchronized 与锁升级

JDK 6 后 synchronized 引入锁升级路径:无锁 → 偏向锁 → 轻量级锁(CAS 自旋)→ 重量级锁(monitor)。锁只能升级不能降级(JDK 15 起偏向锁默认禁用)。

### volatile 与 JMM

- `volatile` 保证可见性与禁止指令重排,通过内存屏障实现,但不保证原子性。
- happens-before 规则是 JMM 判断数据是否有竞争的核心依据。

### AQS

`AbstractQueuedSynchronizer` 是 J.U.C 的基石,采用 `volatile int state` + CLH 变体的双向等待队列。ReentrantLock、Semaphore、CountDownLatch 都基于它实现。

```java
// ReentrantLock 公平锁示例
ReentrantLock lock = new ReentrantLock(true);
lock.lock();
try {
    // 临界区
} finally {
    lock.unlock(); // 必须在 finally 中释放
}
```

## 常见面试要点

1. String 为什么设计成不可变?——字符串常量池、hashCode 缓存、线程安全。
2. `==` 与 `equals` 区别;重写 `equals` 必须重写 `hashCode`。
3. ThreadLocal 内存泄漏原因:ThreadLocalMap 的 key 是弱引用,value 是强引用,线程池场景用完必须 `remove()`。
