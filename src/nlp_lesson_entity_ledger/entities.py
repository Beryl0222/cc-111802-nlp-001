"""课案实体台账的公共领域模型与常量。

状态词汇同时提供常见近义写法（pending / ambiguous / unresolved），
调用方既可以使用数据类，也可以直接操作 to_dict() 得到的普通字典。
"""
from dataclasses import dataclass, field, asdict
from typing import Any

# 实体总体状态
CONFIRMED = "confirmed"
UNCERTAIN = "uncertain"
MERGED = "merged"
ACTIVE_STATUSES = (CONFIRMED, UNCERTAIN)

# 提及链接状态
RESOLVED = "resolved"
PENDING = "pending"
AMBIGUOUS = "ambiguous"
UNRESOLVED = "unresolved"

# 自动/人工判定来源
EXACT = "exact"
ALIAS = "alias"
PROVIDED = "provided"
MANUAL = "manual"
SIMILAR = "similar"
COLLISION = "collision"
NONE = "none"

# 关系状态
CURRENT = "current"
HISTORICAL = "historical"
RETRACTED = "retracted"

ACTION_ALIASES = {
    "link": "link",
    "assign": "link",
    "resolve": "link",
    "resolve_mention": "link",
    "set_entity": "link",
    "attach": "link",
    "确认同一": "link",
    "关联": "link",
    "unlink": "unlink",
    "unassign": "unlink",
    "unresolve": "unlink",
    "detach": "unlink",
    "解除关联": "unlink",
    "merge": "merge",
    "merge_entities": "merge",
    "merge_into": "merge",
    "合并": "merge",
    "split": "split",
    "split_entity": "split",
    "拆分": "split",
    "rename": "rename",
    "rename_entity": "rename",
    "改名": "rename",
    "alias": "add_alias",
    "add_alias": "add_alias",
    "register_alias": "add_alias",
    "别名": "add_alias",
    "confirm": "confirm",
    "confirm_entity": "confirm",
    "确认": "confirm",
    "register_entity": "register_entity",
    "create_entity": "register_entity",
    "add_entity": "register_entity",
    "add_relation": "add_relation",
    "assert_relation": "add_relation",
    "relate": "add_relation",
    "relation": "add_relation",
    "建立关系": "add_relation",
    "remove_relation": "remove_relation",
    "retract_relation": "remove_relation",
    "delete_relation": "remove_relation",
    "撤销关系": "remove_relation",
}

KIND_GROUPS = [
    {"student", "students", "pupil", "learner", "learners", "学生", "学生组", "小组", "学习者"},
    {"equipment", "device", "instrument", "tool", "apparatus", "器材", "实验器材", "设备", "仪器", "工具", "装置"},
    {"concept", "knowledge", "notion", "topic", "概念", "知识点", "知识概念", "原理"},
    {"material", "substance", "材料", "药品", "试剂"},
    {"person", "teacher", "教师", "老师"},
]


def normalize_action(action: str) -> str:
    if action is None:
        raise ValueError("纠正操作缺少 action")
    key = str(action).strip().lower().replace("-", "_").replace(" ", "_")
    if key in ACTION_ALIASES:
        return ACTION_ALIASES[key]
    if action in ACTION_ALIASES:  # 中文键
        return ACTION_ALIASES[action]
    raise ValueError(f"不支持的人工纠正操作: {action}")


def kinds_compatible(left: str, right: str) -> bool:
    if not left or not right:
        return True
    a = str(left).strip().lower()
    b = str(right).strip().lower()
    if a == b:
        return True
    for group in KIND_GROUPS:
        if a in {x.lower() for x in group} and b in {x.lower() for x in group}:
            return True
    return False


def _pick(data: dict, *keys, default=None):
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return default


@dataclass
class Entity:
    entity_id: str
    canonical_name: str
    kind: str = ""
    status: str = UNCERTAIN
    aliases: list = field(default_factory=list)
    former_ids: list = field(default_factory=list)
    mention_ids: list = field(default_factory=list)
    discriminators: list = field(default_factory=list)
    note: str = ""
    created_by: str = "auto"
    merged_into: str = ""

    @classmethod
    def from_dict(cls, value: Any):
        if isinstance(value, Entity):
            return value
        if not isinstance(value, dict):
            raise ValueError("Entity 需要字典或 Entity 实例")
        return cls(
            entity_id=str(_pick(value, "entity_id", "id", "entityId")),
            canonical_name=str(_pick(value, "canonical_name", "name", "label", "canonicalName")),
            kind=str(_pick(value, "kind", "type", "entity_type", "entityType", default="") or ""),
            status=str(_pick(value, "status", default=UNCERTAIN)),
            aliases=list(_pick(value, "aliases", "alias_names", default=[]) or []),
            former_ids=list(_pick(value, "former_ids", "merged_ids", default=[]) or []),
            mention_ids=list(_pick(value, "mention_ids", default=[]) or []),
            discriminators=list(_pick(value, "discriminators", "contexts", default=[]) or []),
            note=str(_pick(value, "note", "description", default="") or ""),
            created_by=str(_pick(value, "created_by", default="auto")),
            merged_into=str(_pick(value, "merged_into", default="") or ""),
        )

    def to_dict(self):
        data = asdict(self)
        # 常见近义键，方便不同调用约定读取。
        data["id"] = self.entity_id
        data["name"] = self.canonical_name
        data["label"] = self.canonical_name
        data["type"] = self.kind
        data["entity_type"] = self.kind
        data["uncertain"] = self.status == UNCERTAIN
        data["confirmed"] = self.status == CONFIRMED
        return data


@dataclass
class Mention:
    mention_id: str
    record_id: str
    revision: int
    text: str
    start: int = -1
    end: int = -1
    snippet: str = ""
    kind: str = ""
    entity_id: str | None = None
    status: str = UNCERTAIN
    resolution: str = NONE
    confidence: float = 0.0
    candidate_ids: list = field(default_factory=list)
    proposed_entity_id: str | None = None
    occurrence: int = 0
    client_id: str = ""
    discriminator: str = ""
    correction_id: str = ""

    def to_dict(self):
        data = asdict(self)
        data["id"] = self.mention_id
        data["name"] = self.text
        data["type"] = self.kind
        # 近义状态/字段，兼容 pending / unresolved 等表述。
        data["state"] = self.status
        data["needs_review"] = self.status == UNCERTAIN
        data["is_uncertain"] = self.status == UNCERTAIN
        data["resolved"] = self.status == RESOLVED
        data["pending"] = self.status == UNCERTAIN
        data["unresolved"] = self.status == UNCERTAIN and self.entity_id is None
        data["candidates"] = list(self.candidate_ids)
        return data


@dataclass
class Correction:
    correction_id: str
    seq: int
    action: str
    payload: dict = field(default_factory=dict)
    author: str = "reviewer"
    created_at: str = ""
    note: str = ""

    def to_dict(self):
        data = asdict(self)
        data["id"] = self.correction_id
        data["at"] = self.created_at
        data["revision"] = self.seq
        data["decided_revision"] = self.seq
        return data


@dataclass
class Relation:
    relation_id: str
    source: str | None
    target: str | None
    relation: str
    status: str = UNCERTAIN
    evidence: list = field(default_factory=list)
    subject_name: str = ""
    object_name: str = ""
    current: bool = False

    def to_dict(self):
        data = asdict(self)
        data["id"] = self.relation_id
        data["predicate"] = self.relation
        data["type"] = self.relation
        data["name"] = self.relation
        data["source_entity_id"] = self.source
        data["target_entity_id"] = self.target
        data["source_id"] = self.source
        data["target_id"] = self.target
        data["from_id"] = self.source
        data["to_id"] = self.target
        data["subject_id"] = self.source
        data["object_id"] = self.target
        data["subject"] = self.subject_name
        data["object"] = self.object_name
        data["provenance"] = list(self.evidence)
        data["present_in_latest_version"] = self.current
        return data


# 常见近义类名
EntityIdentity = Entity
EntityRecord = Entity
EntityNode = Entity
MentionRecord = Mention
EntityMention = Mention
MentionSpan = Mention
ManualCorrection = Correction
Decision = Correction
CorrectionRecord = Correction
RelationTriple = Relation
Triple = Relation
ConceptRelation = Relation
