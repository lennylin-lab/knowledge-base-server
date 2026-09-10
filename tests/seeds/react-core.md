---
title: "React 核心概念:Hooks、渲染模型与状态管理"
tags:
  - react
  - frontend
  - framework
---

# React 核心概念:Hooks、渲染模型与状态管理

## 组件与 JSX

React 用「UI = f(state)」的声明式模型描述界面,JSX 编译为 `createElement`/`jsx` 调用生成元素树。组件首字母必须大写;列表渲染需要稳定的 `key`(避免用数组索引,插入删除时会引起状态错位)。

```jsx
function TodoList({ todos, onToggle }) {
  return (
    <ul>
      {todos.map((t) => (
        <li key={t.id}>
          <label style={{ textDecoration: t.done ? 'line-through' : 'none' }}>
            <input type="checkbox" checked={t.done}
                   onChange={() => onToggle(t.id)} />
            {t.title}
          </label>
        </li>
      ))}
    </ul>
  )
}
```

## Hooks 核心用法

```jsx
import { useState, useEffect, useMemo, useCallback, useRef } from 'react'

function Search({ keyword }) {
  const [data, setData] = useState(null)

  useEffect(() => {
    const ctrl = new AbortController()
    fetch(`/api/search?q=${keyword}`, { signal: ctrl.signal })
      .then((r) => r.json())
      .then(setData)
    return () => ctrl.abort()  // 清理副作用,防止竞态与泄漏
  }, [keyword])

  const heavy = useMemo(() => transform(data), [data])
  const onSubmit = useCallback((e) => e.preventDefault(), [])

  return <form onSubmit={onSubmit}>{/* ... */}</form>
}
```

要点:

1. 依赖数组要诚实;React 18 严格模式开发态会双调用 effect,依赖写对才能安全。
2. `useMemo`/`useCallback` 用于昂贵计算或给 memo 子组件传稳定引用,滥用反而增加开销。
3. `useRef` 存可变值与 DOM 引用,不触发渲染。
4. React 19:`use` + Actions(`useTransition`/`useOptimistic`)简化异步流程,forwardRef 不再必须。

## 渲染与并发

渲染流程:state 变化 → 触发 render(生成新元素树)→ reconcile(diff)→ commit(DOM 更新)。React 18 的并发特性:

- **自动批处理**:事件外的多次 setState 也合并。
- **Transition**:`startTransition` 把更新标记为可中断的低优先级,输入响应不被大列表渲染阻塞。
- **Suspense**:数据获取与代码分割的统一「加载中」语义。

优化三件套:`React.memo`(子组件浅比较)、`useMemo`/`useCallback` 稳定 props、状态下放(colocation)。先用 React DevTools Profiler 定位真实瓶颈再动手。

## 状态管理选型

| 方案 | 适用 |
| --- | --- |
| useState/useReducer + Context | 局部/低频全局状态 |
| TanStack Query(React Query) | 服务端状态:缓存、重试、失效、乐观更新 |
| Zustand | 轻量全局 store,无样板代码 |
| Redux Toolkit | 复杂业务流、强 devtools/中间件需求 |
| jotai / recoil | 原子化细粒度状态 |

心智模型:**服务端状态交给 TanStack Query,客户端状态能用局部就不用全局**。

## 路由与工程化

- React Router v6/7:`createBrowserRouter`、loader(数据预取)、嵌套路由 `<Outlet />`。
- 元框架:Next.js(App Router、RSC 服务端组件、SSR/ISR)与 Remix,做 SEO 或全栈时首选。
- 测试:Vitest + React Testing Library(测行为不测实现)+ Playwright。

## 与 Vue 的差异速记

- Vue 响应式自动追踪依赖,React 显式 setState 触发与依赖数组声明。
- Vue 模板编译期优化多;React JSX 全运行时(靠编译器 React Compiler 补齐)。
- 生态:React 生态更大更碎片化,Vue 官方全家桶更收敛。
