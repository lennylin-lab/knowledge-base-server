---
title: "Flutter 跨平台开发:Widget 体系、状态管理与性能"
tags:
  - flutter
  - mobile
  - frontend
  - dart
---

# Flutter 跨平台开发:Widget 体系、状态管理与性能

## 为什么是 Flutter

Flutter 用自绘引擎(Skia / Impeller)直接渲染 UI,不依赖平台原生控件,保证多端像素级一致;Dart 语言 AOT 编译为机器码,配合 JIT 热重载(hot reload)。一套代码覆盖 iOS、Android、Web、桌面。

## Widget 体系:一切皆 Widget

```dart
class CounterView extends StatelessWidget {
  const CounterView({super.key, required this.count, required this.onTap});

  final int count;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          Text('count: $count'),
          FilledButton(onPressed: onTap, child: const Text('+1')),
        ],
      ),
    );
  }
}
```

三层核心概念:

1. **Widget**:不可变的配置描述(轻量,频繁重建)。
2. **Element**:Widget 与 RenderObject 之间的实例,持有生命周期与状态,复用靠 `runtimeType + key` 匹配。
3. **RenderObject**:负责布局、绘制、命中测试。

StatelessWidget 与 StatefulWidget 的分界:状态是否需要变化。Stateful 的 `setState` 标记脏 Element,下一帧重新 build。

## 常用布局与导航

- 布局:`Row`/`Column`(主轴/交叉轴)、`Stack`(层叠)、`Expanded`/`Flexible`(弹性)、`ListView.builder`(懒加载列表)、`CustomScrollView` + Sliver(复杂滚动)。
- 导航 2.0:`go_router` 声明式路由,支持深链、嵌套导航与 Web 地址。

```dart
final router = GoRouter(routes: [
  GoRoute(path: '/', builder: (c, s) => const HomePage()),
  GoRoute(path: '/detail/:id', builder: (c, s) => DetailPage(id: s.pathParameters['id']!)),
]);
```

## 状态管理选型

| 方案 | 特点 | 适用 |
| --- | --- | --- |
| setState | 官方内置 | 局部状态、demo |
| Provider | InheritedWidget 封装 | 简单依赖注入 + 状态 |
| Riverpod | 编译安全、可测试、无 BuildContext 依赖 | 中大型应用主流选择 |
| Bloc | 事件驱动、强结构 | 团队规范严格、复杂业务流 |
| GetX | 全家桶但争议较大 | 快速原型 |

新手主线推荐 Riverpod:状态声明为 Provider,Widget 用 `ref.watch` 订阅,粒度精确,天然可单测。

## 与原生通信:Platform Channel

```dart
static const channel = MethodChannel('samples.flutter.dev/battery');

Future<int> getBatteryLevel() async =>
    await channel.invokeMethod<int>('getBatteryLevel') ?? -1;
```

原生侧注册 `FlutterMethodChannel` 同名通道处理调用;高频数据(传感器、音视频)用 `EventChannel` 流式传输或直接 FFI。

## 性能优化清单

1. 构建 Distribute:用 `const` 构造函数减少重建;把 `build` 拆小,用子 Widget 而不是 helper 方法(子 Widget 有独立 Element 可复用)。
2. 列表:`ListView.builder` 懒加载;固定行高给 `itemExtent`;图片给 `cacheWidth` 降采样。
3. 用 `RepaintBoundary` 隔离频繁重绘区域,动画用 Transform/Opacity(合成层)而不是布局属性。
4. profiling 用 DevTools:Performance 面板看 UI/Raster 线程是否超 16.7ms(120Hz 为 8.3ms);Memory 看图片缓存。
5. 发布构建:`flutter build apk --release`;调试性能必须用 profile/release 模式,debug 模式数据不可信。
