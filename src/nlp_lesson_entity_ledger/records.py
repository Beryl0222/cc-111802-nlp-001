from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class TextRecord:
    record_id: str
    source: str
    revision: int
    observed_at: str
    text: str
    context: str = ""
    # 以下字段全部可选：文本标注环节可以把找到的名称、三元关系随记录一起导入。
    raw_text: str = ""
    mentions: list = field(default_factory=list)
    relations: list = field(default_factory=list)
    names: list = field(default_factory=list)

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict):
            raise ValueError("文本记录必须是对象")
        text = str(value.get("text", ""))
        record = cls(
            record_id=str(value.get("record_id", value.get("id", ""))).strip(),
            source=str(value.get("source", "")).strip(),
            revision=int(value.get("revision", value.get("rev", 0)) or 0),
            observed_at=str(value.get("observed_at", value.get("at", ""))).strip(),
            text=text,
            context=str(value.get("context", "")).strip(),
            raw_text=text,
            mentions=list(value.get("mentions", []) or []),
            relations=list(value.get("relations", []) or []),
            names=list(value.get("names", []) or []),
        )
        if not record.record_id or not record.source or record.revision < 1 or not record.text.strip():
            raise ValueError("文本记录缺少身份、来源、有效修订号或正文")
        datetime.fromisoformat(record.observed_at.replace("Z", "+00:00"))
        return record

    def to_dict(self):
        return asdict(self)


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, (tuple, set)):
        return list(value)
    return [value]


def collect_annotation_payload(value: dict) -> dict:
    """从各种可能的标注格式中收集 mentions / relations，容忍不同字段命名。

    支持把标注放在顶层，或放在 analysis / annotation / annotations 下。
    注意：annotate 生成的 terms/tokens 只是分词结果，不等同于实体提及，
    因此不会被自动当作 mentions。
    """
    mention_values: list = []
    relation_values: list = []
    seen_mentions: set = set()
    seen_relations: set = set()

    mention_keys = ("mentions", "entities", "names", "annotations")
    relation_keys = ("relations", "triples", "links", "facts")

    containers = [value]
    for nested_key in ("analysis", "annotation"):
        nested = value.get(nested_key)
        if isinstance(nested, dict):
            containers.append(nested)

    def _append(target, seen, item):
        marker = repr(item)
        if marker not in seen:
            seen.add(marker)
            target.append(item)

    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in mention_keys:
            for item in _as_list(container.get(key)):
                _append(mention_values, seen_mentions, item)
        for key in relation_keys:
            for item in _as_list(container.get(key)):  # type: ignore[arg-type]
                _append(relation_values, seen_relations, item)

    return {"mention_specs": mention_values, "relation_specs": relation_values}


def prepare_record(value: dict) -> dict:
    """校验原始导入记录，并把各种标注写法归并到 mentions / relations。"""
    record = TextRecord.from_dict(value)
    data = record.to_dict()
    collected = collect_annotation_payload(value if isinstance(value, dict) else {})
    data["mentions"] = collected["mention_specs"]
    data["relations"] = collected["relation_specs"]
    return data
