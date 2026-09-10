---
title: "Go Web 框架选型:Gin、go-zero 与 Kratos"
tags:
  - golang
  - framework
  - microservices
---

# Go Web 框架选型:Gin、go-zero 与 Kratos

## Gin:事实标准的 HTTP 框架

Gin 以 radix tree 路由 + 高性能中间件生态成为 Go Web 的事实标准。

```go
func main() {
    r := gin.Default() // 自带 Logger + Recovery

    api := r.Group("/api/v1")
    api.Use(RateLimit())
    {
        api.POST("/documents", createDocument)
        api.GET("/documents/:id", getDocument)
    }

    _ = r.Run(":8080")
}

func getDocument(c *gin.Context) {
    id := c.Param("id")
    var q struct {
        Limit int `form:"limit,default=20" binding:"gte=1,lte=100"`
    }
    if err := c.ShouldBindQuery(&q); err != nil {
        c.JSON(400, gin.H{"error": err.Error()})
        return
    }
    c.JSON(200, gin.H{"id": id, "limit": q.Limit})
}
```

要点:`c.Request.Context()` 贯穿下游调用;`ShouldBind` 系列(而不是 `Bind`)便于自定义错误处理;生产替换 Recovery 为带告警的 recovery 中间件。

## go-zero:一站式微服务框架

go-zero 内置代码生成(goctl)、API/RPC 服务、熔断降级、限流、监控:

```bash
goctl api go -api user.api -dir .   # 从 .api 文件生成服务骨架
```

核心组件:zerolog 日志、sqlx(内置缓存旁路 cache control)、负载均衡客户端 zrpc(基于 gRPC)、内置 breaker 与 shedding(过载保护)。适合团队想「开箱即用、规范统一」的场景。

## Kratos:B 站开源的 gRPC 优先框架

Kratos 主推 Protobuf 定义一切(API、配置、错误码、依赖注入 wire),分层清晰(transport/business/data),适合大型多团队项目,学习曲线比 go-zero 陡。

## 选型对比

| 维度 | Gin | go-zero | Kratos | 标准库 net/http |
| --- | --- | --- | --- | --- |
| 心智负担 | 低 | 中 | 高 | 最低 |
| 生态中间件 | 最丰富 | 内置全家桶 | 中等 | 需自建 |
| 微服务配套 | 自组 | 强(api+rpc+devops) | 强(proto 驱动) | 无 |
| 性能 | 高 | 高 | 高 | 高 |

Go 1.22+ 的 `net/http` 增强(路由通配符 `GET /items/{id}`、方法匹配)让小服务可以零依赖直写;需要中间件生态再上 Gin;团队级微服务规范选 go-zero 或 Kratos。

## Go 项目通用工程实践

1. **项目布局**:`cmd/` 入口、`internal/` 私有包、`pkg/` 可对外库;避免超大 `utils` 包,按领域拆分。
2. **优雅退出**:监听 SIGTERM → `http.Server.Shutdown(ctx)` → 关闭连接池与 MQ 消费者。
3. **配置**:viper 或 envconfig,遵循 12-Factor,配置进环境变量。
4. **错误码**:业务错误实现 `code() int` 接口,gRPC 场景映射 `status.Code`;日志里错误带上下文 `%w` 保留链。
5. **压测基线**:`wrk`/`vegeta` 压 P99,配合 pprof 火焰图定位;数据库连接池 `sql.DB.SetMaxOpenConns` 必须显式设置。
