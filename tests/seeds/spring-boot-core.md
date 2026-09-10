---
title: "Spring Boot 核心机制与工程实践"
tags:
  - spring-boot
  - java
  - framework
  - backend
---

# Spring Boot 核心机制与工程实践

## IoC 与依赖注入

控制反转(IoC)把对象的创建与装配交给容器。Bean 的作用域默认 singleton;`@Autowired` 按类型注入,多个候选时配合 `@Primary` 或 `@Qualifier`。

```java
@Service
public class OrderService {
    private final PaymentGateway gateway;

    // 构造器注入:字段 final、便于单测,是官方推荐方式
    public OrderService(PaymentGateway gateway) {
        this.gateway = gateway;
    }
}
```

条件装配是 Spring Boot 自动配置的基石:`@ConditionalOnClass`、`@ConditionalOnMissingBean`、`@ConditionalOnProperty` 等。

## 自动配置原理

`@SpringBootApplication` = `@Configuration` + `@EnableAutoConfiguration` + `@ComponentScan`。自动配置加载 `META-INF/spring/org.springframework.boot.autoconfigure.AutoConfiguration.imports`(2.7+ 新机制)中列出的配置类,再靠条件注解按 classpath 与配置决定是否生效。自定义 starter = 自动配置类 + 注册文件 + 可选属性类(`@ConfigurationProperties`)。

## Web 层与参数校验

```java
@RestController
@RequestMapping("/api/orders")
@Validated
public class OrderController {

    @PostMapping
    public OrderView create(@RequestBody @Valid OrderCreateRequest req) {
        return orderService.create(req);
    }

    @GetMapping("/{id}")
    public OrderView get(@PathVariable UUID id) {
        return orderService.get(id);
    }
}
```

校验用 Jakarta Bean Validation:`@NotNull`、`@Size`、`@Pattern`,配合 `@ControllerAdvice` + `@ExceptionHandler` 统一异常到标准错误信封。

## 事务管理

`@Transactional` 基于 AQS 之上的 AOP 代理实现,注意失效场景:

1. 非 public 方法(代理拦截不到)。
2. 自调用(`this.method()` 不经过代理)。
3. 异常被吞或抛出受检异常(默认只回滚 RuntimeException,需 `rollbackFor = Exception.class`)。
4. 传播行为:`REQUIRED`(默认)、`REQUIRES_NEW`(挂起当前开新事务)、`NESTED`(保存点)。

## 常用组件速查

| 场景 | 组件 |
| --- | --- |
| 数据访问 | Spring Data JPA / MyBatis(-Plus) |
| 缓存 | Spring Cache 抽象(`@Cacheable`/`@CacheEvict`) + Redis |
| 调度 | `@Scheduled` / ShEDLock(分布式锁防重复执行) |
| 异步 | `@Async` + 自定义 ThreadPoolTaskExecutor |
| 配置中心 | Nacos / Apollo + `@RefreshScope` |
| 远程调用 | OpenFeign(声明式 HTTP)、gRPC |
| 可观测 | Micrometer + Prometheus、SkyWalking |

## 性能与排障

- 启动优化:懒加载 `spring.main.lazy-initialization=true`、减少自动配置扫描;GraalVM Native Image 追求极致启动。
- 线程池:Tomcat `server.tomcat.threads.max` 默认 200,结合下游 RT 与压测调整。
- 内存泄漏排查:`jmap -histo` → MAT 分析 dump → 关注 ClassLoader 泄漏与大集合缓存。
- GC 排障:先 `-Xlog:gc*:file=gc.log` 采集,再判断是停顿问题(换 ZGC)还是吞吐问题(调 Parallel/G1 参数)。
- Actuator 暴露 `/health`、`/metrics`、`/prometheus`,生产务必加固访问控制。
