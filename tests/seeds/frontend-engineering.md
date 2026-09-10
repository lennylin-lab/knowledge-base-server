---
title: "前端工程化:构建工具、TypeScript 与 Monorepo"
tags:
  - frontend
  - engineering
  - typescript
---

# 前端工程化:构建工具、TypeScript 与 Monorepo

## 构建工具:从 Webpack 到 Vite

**Webpack** 以 loader(转换资源)+ plugin(干预构建流程)模型统治了上一代前端;代价是 dev 阶段整包打包,启动慢。

**Vite** 的思路:

- 开发态:原生 ESM 按需加载,esbuild(Go 编写)预打包依赖,毫秒级冷启动。
- 生产态:Rollup 打包(Rolldown 正在用 Rust 统一两侧)。
- 热更新 HMR 只替换变更模块,精确到组件。

```ts
// vite.config.ts
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    proxy: { '/api': { target: 'http://localhost:8000', changeOrigin: true } },
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks: { vendor: ['vue', 'vue-router', 'pinia'] },
      },
    },
  },
})
```

Rspack(Rust 版 Webpack)/ Turbopack 面向存量 Webpack 大型项目的迁移提速;Turbopack 已内置于 Next.js。

## TypeScript 关键实践

```ts
// discriminated union 代替可选字段堆砌
type Result<T> =
  | { ok: true; value: T }
  | { ok: false; error: string }

function handle<T>(r: Result<T>) {
  if (r.ok) return r.value  // 收窄为成功分支
  throw new Error(r.error)
}

// satisfies:校验类型但保留字面量推断
const routes = {
  home: '/',
  detail: '/doc/:id',
} satisfies Record<string, string>
```

1. `strict: true` 是起点;`any` 换成 `unknown` + 收窄。
2. 类型体操适度:`Partial`、`Pick`、`Omit`、`Awaited`、模板字面量类型覆盖 80% 场景。
3. `tsc --noEmit` 进 CI;`zod` 做「运行时边界」校验(API 响应、表单),推导静态类型 `z.infer<typeof schema>`。

## 代码质量链路

- **ESLint**(flat config)+ **Prettier**:风格不进 Code Review;`eslint-plugin-import` 管循环依赖与顺序。
- **Git hooks**:husky + lint-staged 只检查暂存文件;pre-commit 跑 lint,pre-push 跑测试。
- **提交规范**:Conventional Commits(`feat:`/`fix:`/`chore:`)配合 commitlint 与自动 CHANGELOG。
- **CI 门禁**:type-check + lint + unit test + build 四件套,PR 粒度执行。

## Monorepo 与包管理

| 工具 | 特点 |
| --- | --- |
| pnpm workspace | 硬链接省磁盘、严格依赖(治幽灵依赖),通用首选 |
| Turborepo | 任务图 + 远程缓存,增量构建 |
| Nx | 依赖图分析、代码生成、多语言 |

```yaml
# pnpm-workspace.yaml
packages:
  - "apps/*"
  - "packages/*"
```

Monorepo 收益:原子提交跨包改动、统一依赖版本、共享配置;代价:构建编排与缓存基础设施。中型以上多包项目 pnpm + Turborepo 是当前主流组合。

## 浏览器侧性能指标

以 Core Web Vitals 为纲:

- **LCP**(最大内容绘制)< 2.5s:关键图 preload、SSR/SSG、资源优先级 `fetchpriority`。
- **INP**(交互到下一帧)< 200ms:拆长任务(`scheduler.yield`)、减少主线程 JS。
- **CLS**(累积布局偏移)< 0.1:图片/广告位定尺寸,字体 `font-display: swap`。
- 资源层:代码分割 + 路由级懒加载、图片 AVIF/WebP、关键 CSS 内联、HTTP 缓存(强缓存文件名 hash + 协商缓存 index)。
