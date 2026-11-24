# 课案实体台账

课案实体台账用于处理本地合成教研文本。它在原有“导入记录 → 规范化/标注 → 列表查询”
之上，把 **实体身份、各版本中的提及、人工纠正** 与 **带溯源的关系查询** 放在同一份
本地 JSON 台账中，服务后续的概念关系图整理。

- 同一 `record_id` 的不同 `revision` 表示同一份课案的不同年级/修订版本；
- 每个版本里的名称是一条 **提及（Mention）**，带有起止位置与 **原文片段（snippet）**；
- 提及归属于稳定的 **实体身份（Entity）**；无法可靠延续身份时保持 `uncertain`，
  只给出候选身份，绝不自动合并简称或同名的不同器材；
- **人工纠正（Correction）** 是只追加的决定日志；实体/提及/关系都是
  “版本 + 纠正日志”确定性重放的派生状态。重新处理或重新导入旧版本后，
  人工纠正会重新生效，较新的人工决定不会被自动结果覆盖；
- 关系查询的每条证据都能查到 **记录、版本/修订号、原文片段**，
  人工建立的关系还能查到 **哪条纠正、哪次决定序号**。

## 环境

需要 Python 3.11 或更新版本。应用运行不依赖第三方包；运行测试和构建前，安装固定版本的开发工具：

```bash
python -m pip install -r requirements-dev.txt
```

## 快速体验

```bash
# 原始演练记录（原有功能）
PYTHONPATH=src python -m nlp_lesson_entity_ledger.cli ingest fixtures/records.json
PYTHONPATH=src python -m nlp_lesson_entity_ledger.cli list

# 多版本课改示例 + 人工合并纠正
STATE=/tmp/lesson-ledger.json
PYTHONPATH=src python -m nlp_lesson_entity_ledger.cli ingest fixtures/lesson_versions.json --state "$STATE"
PYTHONPATH=src python -m nlp_lesson_entity_ledger.cli mentions --state "$STATE" --status uncertain
PYTHONPATH=src python -m nlp_lesson_entity_ledger.cli correct fixtures/corrections.json --state "$STATE"
PYTHONPATH=src python -m nlp_lesson_entity_ledger.cli relations --state "$STATE" --entity 电源
PYTHONPATH=src python -m nlp_lesson_entity_ledger.cli entities --state "$STATE"
```

运行数据默认保存在 `var/records.json`，可用 `--state` 指定其他路径。

## Python 用法

```python
from nlp_lesson_entity_ledger import TextWorkflow, RecordRepository

wf = TextWorkflow(RecordRepository("var/ledger.json"))
wf.ingest(records)            # 与旧接口一致：{"received": n, "stored": n}

wf.list_entities(status="uncertain")
wf.list_mentions(record_id="electricity-lesson")
wf.uncertain_mentions()       # 待复核队列

# 人工确认“安培表/电流表”“电池/电源”是同一对象
wf.merge_entities(source="安培表", target="电流表", author="教研组长")
wf.add_alias(alias="电池", entity="电源")
wf.link_mention(mention="m2", entity="ent-6")
wf.confirm_entity("ent-6")

# 关系查询与溯源
relations = wf.query_relations(entity="电源", relation_type="点亮")
for rel in relations:
    for evidence in rel["evidence"]:
        print(evidence["record_id"], evidence["revision"],
              evidence["snippet"], evidence["correction_id"])

wf.relation_provenance(rel["relation_id"])
wf.trace_entity("电流表")      # 实体 + 各版本提及 + 纠正 + 关系时间线
wf.concept_graph()            # {"nodes": [...], "edges": [...]}
wf.reprocess("electricity-lesson", 1)   # 重跑自动派生，纠正不丢失
```

`wf.entities`、`wf.mentions`、`wf.relations` 既是列表，也可以调用过滤，例如
`wf.entities(status="confirmed")`、`wf.relations(current_only=True)`。

### 记录中可携带的标注字段

导入记录在原有字段外，还可选携带：

```json
{
  "mentions": [
    {"id": "m1", "text": "电流表", "kind": "器材",
     "start": 12, "occurrence": 0, "discriminator": "分组 A"}
  ],
  "relations": [
    {"subject": "第一小组", "relation": "使用", "object": "电流表",
     "snippet": "第一小组使用电流表"}
  ]
}
```

也兼容：纯字符串提及、`[主, 关系, 客]` 三元组、放在 `analysis.mentions` 中的标注，
以及 `subject/source/from`、`object/target/to`、`predicate/type` 等常见字段命名。
没有显式提及时，只有显式提供 `gazetteer` 才会做词典匹配，默认不猜测中文名称。

## 不确定状态与合并原则

| 情况 | 处理 |
| --- | --- |
| 名称/已登记别名唯一精确命中 | 自动链接，`resolution="exact"/"alias"` |
| 首次出现的新名称 | 建立 `uncertain` 候选身份，提及 `entity_id=None`、`proposed_entity_id=<候选>` |
| 简称/全称相似（如“滑动变阻器/变阻器”） | 仅给 `candidate_ids`，状态保持 `uncertain`，不自动合并 |
| 同名但 `kind`/`discriminator` 冲突（同名不同器材） | 标记 `ambiguous` 与候选，另立身份，不合并 |
| 人工 `link/merge/alias/confirm` | 提及转为 `resolved`，身份转为 `confirmed`，重放时始终优先 |

## 持久化结构

状态文件是一个 JSON 对象，除向后兼容的 `records`（每个 `record_id` 的最新修订）外，
还包含 `versions`、`entities`、`mentions`、`corrections`、`relations`。
写入采用临时文件 + 原子替换；整批导入会先校验全部记录，任何一条无效都不会部分写盘。

## 测试

执行 `python -m pytest -q` 检查文本规范化、记录处理、状态读取、身份延续、
人工纠正优先级、不确定性、关系溯源及本地持久化。
