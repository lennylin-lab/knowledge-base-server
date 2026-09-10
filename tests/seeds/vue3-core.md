---
title: "Vue 3 核心机制:响应式、组合式 API 与生态"
tags:
  - vue
  - frontend
  - framework
---

# Vue 3 核心机制:响应式、组合式 API 与生态

## 响应式原理:Proxy

Vue 3 用 ES `Proxy` 重写响应式系统(替代 Vue 2 的 `Object.defineProperty`):

- 支持动态新增/删除属性、数组索引与 length 修改,不再需要 `Vue.set`。
- 惰性深代理:嵌套对象在访问时才被代理,初始化开销更小。
- 三个核心 API:`reactive`(对象代理)、`ref`(带 `.value` 的包装,基本类型也支持)、`shallowRef`/`shallowReactive`(浅层)。

```js
import { reactive, ref, computed, watchEffect } from 'vue'

const count = ref(0)
const state = reactive({ list: [] })
const double = computed(() => count.value * 2)

watchEffect(() => {
  console.log(`count is ${count.value}`) // 依赖被收集,变化时重新执行
})
```

常见坑:`ref` 解构会丢失响应式,用 `toRefs(state)` 转换后解构;`reactive` 整体替换对象引用会断开代理,尽量逐字段赋值。

## 组合式 API 与组件通信

```vue
<script setup>
import { defineProps, defineEmits } from 'vue'

const props = defineProps({ modelValue: String })
const emit = defineEmits(['update:modelValue'])
</script>

<template>
  <input :value="props.modelValue"
         @input="emit('update:modelValue', $event.target.value)" />
</template>
```

`<script setup>` 是编译期语法糖,变量直接暴露给模板,性能优于运行时 setup 返回对象。通信方式速查:

1. 父→子:props;子→父:emit。
2. v-model 双向绑定:`modelValue` + `update:modelValue` 约定。
3. 跨层级:`provide` / `inject`。
4. 全局状态:Pinia。

## Pinia:官方状态管理

```js
export const useCounterStore = defineStore('counter', () => {
  const count = ref(0)
  const double = computed(() => count.value * 2)
  function increment() { count.value++ }
  return { count, double, increment }
})
```

Pinia 放弃了 Vuex 的 mutation,action 直接改 state;完整的 TS 类型推导与 devtools 支持;setup 语法与组合式 API 心智一致。

## 渲染机制与性能优化

编译期优化是 Vue 3 性能的护城河:**静态提升**(hoistStatic)、**Patch Flag**(编译时标记动态节点类型,运行时只 diff 动态部分)、**Block Tree**(扁平化动态节点收集)。

常用优化手段:

1. 大列表用 `v-memo` 或虚拟滚动(vue-virtual-scroller)。
2. `v-if` 与 `v-show` 选择:切换频繁用 v-show(display 切换),条件少见销毁用 v-if。
3. `computed` 缓存替代模板里的方法调用。
4. 路由组件 `defineAsyncComponent` / 动态 import 做代码分割。
5. keep-alive 缓存组件状态,配合 `include` 控制范围。

## 生态速览

- **路由**:vue-router 4,`createWebHistory`、导航守卫 `beforeEach` 做鉴权、`<router-view>` 配合 transition。
- **构建**:Vite 官方默认,开发毫秒级冷启动。
- **SSR**:Nuxt 3(文件路由、自动导入、混合渲染)。
- **组件库**:Element Plus、Ant Design Vue、Naive UI、Vuetify 3。
- **测试**:Vitest(单测,与 Vite 同源配置)+ Vue Test Utils + Playwright(E2E)。
