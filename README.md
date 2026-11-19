# 课案实体台账

课案实体台账用于处理本地合成教研文本,保存来源版本、文本片段和处理记录,供校内复核使用。

同一份课案会被反复改成适合不同年级的版本,学生、实验器材和知识概念常换简称。台账在文本标注
找出名称的基础上,继续维护**实体身份**:哪些提及仍指向同一对象、哪些是同名不同物、哪些还
无法确定。实体、各版本提及和人工纠正共同支持关系查询。

## 工作方式

版本摄取(自动处理)与人工纠正都记录为带序号的事件,实体、提及、关系由事件日志重建:

- 人工纠正可以随时到达(包括晚于对应版本的自动处理),始终优先于同目标的自动结果;
- 重新处理旧版本只替换该版本的摄取事件,不会覆盖较新的人工决定;
- 无法可靠延续身份时保留不确定状态:同名多实体的提及列出候选,疑似改名只给出建议,
  候选关系标记为 `uncertain`,均等待人工判定;
- 每条关系的证据都记录来源记录、修订号、原文片段、处理批次和提及标识,可逐项复核。

### 纠正类型

| 类型 | 含义 |
| --- | --- |
| `create_entity` | 登记一个人工实体(可带 `kind`,如 学生/器材/概念) |
| `add_alias` | 给实体增加简称;该名称此后的提及指向它 |
| `merge_entities` | 确认两个实体是同一对象(如简称与全称),提及与关系随之合并 |
| `confirm_distinct` | 确认两个实体不是同一对象,消除改名建议 |
| `assign_mention` | 指定某条提及指向哪个实体 |
| `reject_entity` | 否定某条提及的自动归属,提及回到不确定状态 |

提及标识形如 `lesson-a@r1:学电#0`(记录、修订号、名称、出现序号),可通过 `mentions` 命令查询。

## 环境

需要 Python 3.11 或更新版本。应用运行不依赖第三方包;运行测试和构建前,安装固定版本的开发工具:`python -m pip install -r requirements-dev.txt`。

## 运行

```bash
# 导入两个版本的课案(第二份含措辞调整与段落删除)
PYTHONPATH=src python -m nlp_lesson_entity_ledger.cli ingest fixtures/records.json
PYTHONPATH=src python -m nlp_lesson_entity_ledger.cli ingest fixtures/records-revision.json

# 查看自动处理结果:实体、不确定提及、改名建议
PYTHONPATH=src python -m nlp_lesson_entity_ledger.cli entities
PYTHONPATH=src python -m nlp_lesson_entity_ledger.cli mentions --status uncertain
PYTHONPATH=src python -m nlp_lesson_entity_ledger.cli summary

# 应用人工纠正(可晚于自动处理到达)
PYTHONPATH=src python -m nlp_lesson_entity_ledger.cli correct fixtures/corrections.json

# 查询关系及其来源版本、原文片段、处理批次
PYTHONPATH=src python -m nlp_lesson_entity_ledger.cli relations --entity auto:学生电源
PYTHONPATH=src python -m nlp_lesson_entity_ledger.cli corrections
```

其他命令:`list` 查看当前文本处理结果(可用 `--source` 过滤),`relations --current-only`
只看最新修订仍有证据的关系。运行数据保存在 `var/records.json`(可用 `--state` 更改)。

## 测试

执行 `python -m pytest -q` 检查文本规范化、提及定位、身份延续、同名歧义、人工纠正优先、
重新处理安全性、关系溯源及本地持久化。
