---
title: "Python 语言特性与工程实践"
tags:
  - python
  - backend
---

# Python 语言特性与工程实践

## 数据模型与内建类型

Python 中一切皆对象,变量本质是名字绑定到对象的引用。需要注意可变默认参数陷阱:

```python
# 反例:默认列表在函数定义时创建一次,多次调用共享
def append_to(item, target=[]):
    target.append(item)
    return target

# 正确写法
def append_to(item, target=None):
    if target is None:
        target = []
    target.append(item)
    return target
```

常用容器的时间复杂度:

- `list`:尾部 append O(1),按值查找 O(n),插入/删除头部 O(n)
- `dict` / `set`:平均 O(1),底层为开放寻址哈希表(Python 3.7 起 dict 保持插入序)
- `collections.deque`:两端操作 O(1),适合做队列
- `heapq`:最小堆,`heapq.heappush` / `heappop` 均为 O(log n)

## 深浅拷贝

```python
import copy

a = [[1, 2], [3, 4]]
b = a                  # 引用,完全同一对象
c = a[:]               # 浅拷贝:外层新建,内层元素仍是引用
d = copy.deepcopy(a)   # 深拷贝:递归复制所有层级
```

## 生成器与迭代器

生成器惰性求值,处理大文件/大集合时显著省内存:

```python
def read_large(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            yield line.strip()

lines = read_large("huge.log")
first_error = next((l for l in lines if "ERROR" in l), None)
```

生成器表达式 `sum(x * x for x in range(10))` 比列表推导省一次中间列表分配。

## 装饰器

```python
import functools
import time

def timed(func):
    @functools.wraps(func)  # 保留原函数 __name__/__doc__
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        result = func(*args, **kwargs)
        print(f"{func.__name__} cost {time.perf_counter() - start:.3f}s")
        return result
    return wrapper
```

带参数的装饰器需要再包一层「装饰器工厂」。标准库 `functools.lru_cache` / `functools.cache` 是最常用的现成装饰器。

## GIL 与并发模型

- **GIL(全局解释器锁)**:同一时刻仅一个线程执行 Python 字节码。CPU 密集型多线程无法并行,应使用 `multiprocessing` 或 `concurrent.futures.ProcessPoolExecutor`。
- **IO 密集型**:线程/协程均可。`asyncio` 在单线程内通过事件循环调度协程,配合 `aiohttp`、`asyncpg` 等异步库获得高并发。
- Python 3.12 起实验性 subinterpreters,PEP 703(移除 GIL)正在推进,但短期内仍需按 GIL 假设写代码。

## 类型注解与 dataclass

```python
from dataclasses import dataclass, field

@dataclass(frozen=True, slots=True)
class Point:
    x: float
    y: float
    labels: list[str] = field(default_factory=list)
```

类型注解配合 `mypy` / `pyright` 静态检查,是大型 Python 项目可维护性的基础。`typing.TypeAlias`、`Protocol`(结构化子类型)是 3.10+ 常用特性。

## 虚拟环境与包管理

- `venv` + `pip` 是标配;`uv` 以 Rust 实现,安装速度比 pip 快一个量级,并提供 lockfile。
- `pyproject.toml` 是项目元数据的标准载体(setuptools / poetry / hatch / uv 均支持)。
- 锁定依赖:`uv lock` / `pip-compile`,保证可重现构建。

## 测试

```python
import pytest

@pytest.fixture
def sample():
    return [1, 2, 3]

def test_sum(sample):
    assert sum(sample) == 6

@pytest.mark.parametrize("n, expected", [(1, 1), (2, 4), (3, 9)])
def test_square(n, expected):
    assert n * n == expected
```

pytest 的 fixture 作用域(`function`/`class`/`module`/`session`)与参数化是写出简洁测试的关键。
