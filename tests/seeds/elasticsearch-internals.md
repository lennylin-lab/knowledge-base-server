---
title: "Elasticsearch 原理、映射设计与查询调优"
tags:
  - elasticsearch
  - search
  - middleware
---

# Elasticsearch 原理、映射设计与查询调优

## 基本概念对照

| Elasticsearch | 类比 MySQL |
| --- | --- |
| Index | Table |
| Document | Row |
| Field | Column |
| Mapping | Schema |
| Query DSL | SQL |

分片(shard)是 Lucene 实例,主分片数在索引创建后不可改(需 reindex 或 split);副本分片提供高可用与读扩展。

## 倒排索引与分词

文本字段经过**分析器**(analyzer = character filter + tokenizer + token filter)切分为词项(term),倒排索引记录 term → 文档列表。中文场景推荐 IK 插件:`ik_max_word` 建索引(细粒度利于召回),`ik_smart` 做查询(粗粒度减少噪音)。

```json
PUT /kb_documents
{
  "settings": {
    "analysis": {
      "analyzer": {
        "code": { "tokenizer": "classic", "filters": ["lowercase", "remove_duplicates"] }
      }
    }
  },
  "mappings": {
    "properties": {
      "title":      { "type": "text", "analyzer": "ik_max_word", "search_analyzer": "ik_smart" },
      "chunk_text": { "type": "text", "analyzer": "ik_max_word" },
      "tags":       { "type": "keyword" },
      "embedding":  { "type": "dense_vector", "dims": 1024 }
    }
  }
}
```

`text` 类型参与分词与相关性打分;`keyword` 类型不分词,用于精确过滤、排序与聚合。

## BM25 相关性打分

BM25 公式核心三个因子:

- **TF**:词频,带饱和参数 `k1`(默认 1.2),出现次数越多得分越高但边际递减。
- **IDF**:逆文档频率,词越稀有越重要。
- **文档长度归一化**:`b`(默认 0.75),长文档相对惩罚。

调参经验:`k1` 调高让高频词更值钱;`b` 调高对长文档惩罚更狠。字段权重通过 `multi_match` 的 `boost` 控制,如 `title^3`。

## 查询DSL:filter 与 query 上下文

- **query 上下文**:计算相关性得分。
- **filter 上下文**:只判断是否匹配,不打分,且结果可被缓存——结构性过滤条件(tenant、status、时间范围)一律放 filter。

```json
{
  "query": {
    "bool": {
      "must":   [{ "multi_match": { "query": "redis 分布式锁", "fields": ["title^3", "content"] } }],
      "filter": [{ "term": { "tags": "middleware" } }],
      "should": [{ "match_phrase": { "content": { "query": "分布式锁", "slop": 2 } } }]
    }
  }
}
```

多字段检索时注意**词项覆盖率**:用户查询包含多个词时,可用 `minimum_should_match` 或 additive 的字段分组 + coverage gate,避免单个冷僻字段的长尾匹配霸榜。

## 深分页与游标

- `from + size` 默认最多 10000(`index.max_result_window`),深分页成本 O(from + size)。
- 翻页用 **search_after**(基于 sort 值的 keyset 分页,推荐);导出全量用 **PIT + search_after** 替代已废弃的 scroll。

## 写入与刷新机制

写入路径:memory buffer + translog → (默认 1s)refresh 生成新 segment 可搜索 → flush 时 translog 落盘并持久化 segment。近实时(NRT)搜索的来源就是 refresh 间隔。

- 批量导入场景:`refresh_interval: -1` + 副本数 0,导完再恢复。
- segment 不可变,删除只是打标记,由后台 merge 真正回收;大量小 segment 会拖慢查询。

## 集群与监控要点

1. 健康度:`_cluster/health`;yellow = 副本未分配,red = 主分片丢失。
2. 分片大小经验值 10–50GB,单节点分片数别失控(常见指引:每 GB 堆 ≤ 20 个分片)。
3. JVM 堆 ≤ 物理内存一半,剩余留给 page cache;GC 用 G1。
4. 慢查询日志 `index.search.slowlog` + `_profile` API 定位慢在哪个阶段。
