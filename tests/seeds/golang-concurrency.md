---
title: "Golang 并发编程与工程实践"
tags:
  - golang
  - concurrency
  - backend
---

# Golang 并发编程与工程实践

## goroutine 与调度器

goroutine 是 Go 运行时托管的轻量级线程,初始栈仅 2KB,可按需增长。GMP 调度模型:

- **G**:goroutine,包含栈与指令指针。
- **M**:机器线程,实际执行者。
- **P**:逻辑处理器,持有可运行 G 的本地队列;`GOMAXPROCS` 默认等于 CPU 核数。

调度特点:工作窃取(work stealing)、基于协作式抢占(Go 1.14 起基于信号)、系统调用阻塞时 P 会与 M 解绑。

```go
go func() {
    fmt.Println("in a goroutine")
}()
```

## channel

channel 是 goroutine 之间通信的首选方式,遵循 CSP 模型:「不要通过共享内存来通信,而要通过通信来共享内存」。

```go
ch := make(chan int, 8)  // 带缓冲
ch <- 1                  // 发送
v := <-ch                // 接收
close(ch)                // 仅由发送方关闭

for v := range ch {      // channel 关闭且排空后循环结束
    _ = v
}
```

原则:

1. **谁发送谁关闭**,多接收方场景用额外的 done channel 或 `sync.Once` 关闭。
2. 向已关闭 channel 发送会 panic;从已关闭的空 channel 接收得到零值。
3. nil channel 的收发永远阻塞,可用于 `select` 中禁用分支。

## select 与超时控制

```go
select {
case v := <-ch:
    use(v)
case <-time.After(2 * time.Second):
    return errors.New("timeout")
case <-ctx.Done():
    return ctx.Err()
}
```

## context:跨 goroutine 的取消传播

```go
ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
defer cancel()

req = req.WithContext(ctx) // HTTP 请求绑定超时
```

context 应作为函数第一个参数一路传递;`context.Value` 只放请求级元数据(trace id、鉴权信息),不要当通用参数通道。

## sync 包常用原语

| 原语 | 用途 |
| --- | --- |
| `sync.Mutex` / `RWMutex` | 互斥/读写锁 |
| `sync.WaitGroup` | 等待一组 goroutine 结束 |
| `sync.Once` | 单例初始化 |
| `sync.Map` | 读多写少、key 集合稳定的并发 map |
| `sync.Pool` | 临时对象复用,减轻 GC 压力 |
| `atomic` 包 | 计数器等无锁原子操作 |

```go
var wg sync.WaitGroup
for i := 0; i < 10; i++ {
    wg.Add(1)
    go func() {
        defer wg.Done()
        work()
    }()
}
wg.Wait()
```

## 常见并发陷阱

1. **循环变量捕获**:Go 1.22 之前 `for i := range` 中闭包共享同一个 `i`,需要在循环体内复制(`i := i`)。1.22 起每次迭代都是新变量。
2. **数据竞争**:并发读写 map 会直接 fatal(不是可恢复的 panic);用 `-race` 参数在测试与 CI 中开启竞态检测。
3. **goroutine 泄漏**:阻塞在无人接收的 channel 上导致 goroutine 永不退出,务必保证每个发送都有对应接收路径或通过 context 取消。
4. **`errgroup` 忽略取消**:`golang.org/x/sync/errgroup` 的 `WithContext` 版本会在首个错误时取消派生 context,记得用 `g.Go` 提交任务并 `ctx` 感知。

## 工程实践

- 错误处理:用 `%w` 包装错误并配合 `errors.Is` / `errors.As` 判断;自定义错误类型实现 `Unwrap()`。
- 泛型(Go 1.18+):`func Map[T, U any](s []T, f func(T) U) []U`,约束用接口 `comparable` 或自定义类型集。
- 模块管理:`go mod tidy`、`go work`(多模块工作区)、最小版本选择(MVS)。
- 性能分析:`pprof`(CPU/heap/goroutine)、`go test -bench`、`trace`;先测量再优化。
