---
title: "FastAPI 异步框架:依赖注入、Pydantic 与性能"
tags:
  - fastapi
  - python
  - framework
  - backend
---

# FastAPI 异步框架:依赖注入、Pydantic 与性能

FastAPI 是基于 Starlette(ASGI)与 Pydantic 的现代 Python Web 框架,主打类型驱动、自动文档(OpenAPI/Swagger)与原生 async 支持。

## 基本路由与请求模型

```python
from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field

app = FastAPI(title="Knowledge Base API")

class DocumentCreate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    content: str = Field(min_length=1)

@app.post("/documents", status_code=status.HTTP_201_CREATED)
async def create_document(payload: DocumentCreate) -> dict:
    if not payload.content.strip():
        raise HTTPException(status_code=422, detail="content is blank")
    return {"title": payload.title or "Untitled"}
```

请求体、查询参数、路径参数都由类型注解自动解析与校验,错误默认返回 422 + 明细。

## 依赖注入(Depends)

依赖是可复用、可测试、可嵌套的工厂函数,常用于鉴权、数据库会话、分页参数:

```python
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session

async def current_user(token: str = Depends(oauth2_scheme),
                       db: AsyncSession = Depends(get_db)) -> User:
    ...

@app.get("/me")
async def me(user: User = Depends(current_user)):
    return user
```

测试时用 `app.dependency_overrides[get_db] = fake_db` 一行替换依赖,无需 mock 深层实现。

## async def 还是 def

这是 FastAPI 最容易踩的坑:

- `async def` 端点运行在事件循环里:**内部必须用异步库**(asyncpg、httpx.AsyncClient、aiofiles);在里面调用同步阻塞函数会卡死整个事件循环。
- 普通 `def` 端点会被丢进线程池执行,阻塞不影响其它请求但占线程。
- 准则:全链路异步或全链路同步,不要在 `async def` 里混入 `requests`/`time.sleep` 这类阻塞调用;无法避免时用 `anyio.to_thread.run_sync`。

## 中间件与生命周期

```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.es = AsyncElasticsearch()   # 启动时初始化
    yield
    await app.state.es.close()            # 优雅关闭

app = FastAPI(lifespan=lifespan)

@app.middleware("http")
async def add_trace_id(request, call_next):
    response = await call_next(request)
    response.headers["x-trace-id"] = uuid4().hex
    return response
```

## 性能与部署

- 校验开销:Pydantic v2 的 Rust 核心(`pydantic-core`)使解析速度提升 5–50 倍,大模型优先 v2。
- 生产部署:`uvicorn`/`hypercorn` 多 worker,或 gunicorn 管理 uvicorn worker;更前一层放 Nginx/Envoy。
- 后台任务:轻量场景用 `BackgroundTasks`;重型任务仍应交给任务队列(arq、celery、dramatiq)。
- 可观测:`sentry-sdk` 集成异常;OpenTelemetry 的 ASGI 中间件做分布式追踪。

## 与 Django / Flask 的取舍

| 维度 | FastAPI | Django | Flask |
| --- | --- | --- | --- |
| 异步 | 原生 async | 3.x 部分支持 | 需扩展 |
| API 文档 | 自动 OpenAPI | DRF + drf-spectacular | flask-smorest |
| 定位 | API 服务、微服务、AI 网关 | 全功能大而全(battery included) | 极简微框架 |
| ORM | 任意(SQLAlchemy/SQLModel) | 内置 Django ORM | 任意 |

Python 后端新 API 项目默认 FastAPI;带后台管理(admin)、完整站点用 Django;原型和小服务用 Flask 依然顺手。
