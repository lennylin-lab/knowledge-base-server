# MCP sidecar 部署指南（stdio → streamable HTTP）

本文说明如何用 opt-in 的 sidecar 容器把 stdio/npx 型 MCP server 桥接成
streamable HTTP，供 `knowledge-base-server` 的 `mcp.json` `"url"` 形式接入。

## 为什么生产不用 stdio/npx 直连

`src/app/mcp/manager.py` 的 stdio 形式会把 MCP server 作为 API 进程的
**子进程**spawn 出来。生产容器化部署下这不可行：

- app 镜像是 `python:3.12-slim`，没有 Node/npx；`npx -y ...` 在容器里直接
  `mcp_server_failed`（该 server 被跳过，其余照常，但工具静默缺失）。
- `npx -y` 是运行时下载：需要 npm registry 网络、可写缓存目录（镜像以
  `nobody` 运行）、版本不固定，且引入供应链面。
- 子进程与 Web 进程同 cgroup，无法独立重启/健康检查，多副本时成倍开销。

因此约定：**stdio server 一律经 sidecar 桥接为 HTTP**；`mcp.json` 在容器
部署里只使用 `"url"` 条目。本地裸机开发（宿主机有 Node）仍可直接用
`"command"` 形式，互不冲突。

## 架构

```text
mcp.json ("url")                        docker compose --profile mcp
    |                                     |
    | streamable HTTP                     | node:22-slim（构建期固定版本）
    v                                     v
knowledge-base-server  <-- HTTP -->  mcp-context7 容器
                                        supergateway --stdio context7-mcp \
                                          --outputTransport streamableHttp
```

- 桥接器是 [supergateway](https://github.com/supercorp-ai/supergateway)，
  镜像定义在 `docker/mcp-sidecar/Dockerfile`。
- 桥接器和目标 MCP 包都在**构建期** `npm install -g` 固定版本，容器运行时
  零下载。
- sidecar 以 stateless 模式运行（每个请求重新初始化 stdio 子进程），没有
  会话过期问题；app 侧 `McpManager` 持有的是长连接客户端。

## 本地开发（API 跑在宿主机）

```bash
docker compose --profile mcp up -d --build mcp-context7
```

sidecar 监听 `http://127.0.0.1:9300/mcp`（仅绑定 loopback）。`mcp.json`
（仓库根目录，gitignored）使用：

```json
{
  "mcpServers": {
    "context7": { "url": "http://127.0.0.1:9300/mcp" }
  }
}
```

context7 的 `CONTEXT7_API_KEY` 可选（无 key 有速率限制）；需要时在 compose
的 `mcp-context7.environment` 注入，supergateway 会把自身环境传给子进程。

## 生产（docker-compose.prod.yml）

app 容器从 `/mcp-config/mcp.json` 读 MCP 配置（`KB_MCP_CONFIG_PATH` 已在
compose 中设置，`./mcp-config` 目录 bind 进容器）。目录不存在时 Docker 会
自动创建为空目录，等价于"没有 mcp.json"——MCP 优雅关闭，启动不受影响；
因此**文件级挂载不可用**（缺文件会变成目录，导致启动崩溃），必须用目录。

```bash
# 一次性准备（在服务器仓库根目录）
mkdir -p mcp-config
cp mcp.json.example mcp-config/mcp.json   # 按需编辑，url 用服务名

# 先跑常规部署（拉镜像、迁移），再启用 sidecar 并重建 app
./scripts/deploy.sh
docker compose -f docker-compose.prod.yml --profile mcp up -d --build
```

`mcp-config/mcp.json` 里 sidecar 的地址用 compose 网络服务名（生产不发布
宿主机端口）：

```json
{
  "mcpServers": {
    "context7": { "url": "http://mcp-context7:9300/mcp" }
  }
}
```

sidecar 镜像在服务器上从仓库 checkout 本地构建（可选组件，不进 GHCR 发布
流水线）。升级 = 改 compose 里 `SUPERGATEWAY_VERSION` / `MCP_VERSION`
build args + `--build` 重建。

## 验证

```bash
docker compose ps mcp-context7          # healthcheck: /mcp 返回任意 HTTP 状态即存活
docker compose logs mcp-context7        # supergateway 启动日志（端口、stdio 子进程）
docker compose logs app | grep mcp_     # 期望 mcp_server_started server=context7 transport=http
```

## 添加另一个 stdio server

在对应 compose 文件里复制一份服务块，改 build args、镜像名和环境。例如
`@modelcontextprotocol/server-filesystem`（内部端口都是 9300，容器隔离，
仅本地开发的宿主机发布端口需要错开，如 `127.0.0.1:9301`）：

```yaml
  mcp-filesystem:
    profiles: ["mcp"]
    build:
      context: ./docker/mcp-sidecar
      args:
        MCP_PACKAGE: "@modelcontextprotocol/server-filesystem"
        MCP_VERSION: "<固定版本>"
    image: kb-mcp-filesystem:<版本标签>
    environment:
      MCP_COMMAND: mcp-server-filesystem
      MCP_ARGS: "/data"          # 常量参数；需要卷挂载时自行补 volumes
    # 本地开发再加 ports: ["127.0.0.1:9301:9300"]
```

`mcp.json` 增加对应 `"url"` 条目即可。`MCP_COMMAND` 必须是该 npm 包的
bin 名（`npm info <pkg> bin` 查询）。

## 故障行为与运维注意

- **restart-to-reload 是设计行为**：`McpManager` 只在进程启动时连接并快照
  工具，改 `mcp.json`、重启 sidecar 后需要重启 app（生产
  `docker compose -f docker-compose.prod.yml restart app`）。
- sidecar 宕机不影响 app 存活：启动时连不上记 `mcp_server_failed` 并跳过
  该 server；恢复 sidecar 后同样要重启 app 才会重连。
- `mcp.json` / `mcp-config/` 均已 gitignore——里面的 `env` 可能带真实
  API key，按本地凭据保护，不要提交或 baking 进镜像。
