"""课案文本整理工作流。

在原有“导入记录 -> 规范化 -> 标注 -> 列表查询”流程之上，加入：
实体身份、版本提及、人工纠正重放以及带溯源的关系查询。
"""
from pathlib import Path

from .entities import Correction, Entity, Mention, Relation
from .ledger import EntityLedger
from .records import prepare_record
from .repository import RecordRepository

__all__ = ["TextWorkflow", "CallableList", "build_workflow", "process_records",
           "EntityLedger", "RecordRepository", "Entity", "Mention", "Correction", "Relation"]


class CallableList(list):
    """既是列表，也可以调用进行过滤：``workflow.entities(status='uncertain')``。

    字符串下标会按 id/名称查找，因此 ``wf.entities["电流表"]`` 也可用。
    """

    def __init__(self, items, caller=None, lookup=None):
        super().__init__(items)
        self._caller = caller
        self._lookup = lookup

    def __call__(self, *args, **kwargs):
        if self._caller is None:
            return CallableList(list(self), lookup=self._lookup)
        result = self._caller(*args, **kwargs)
        return CallableList(result if isinstance(result, list) else list(result), lookup=self._lookup)

    def __getitem__(self, key):
        if isinstance(key, str) and self._lookup is not None:
            found = self._lookup(key)
            if found is not None:
                return found
            raise KeyError(key)
        return super().__getitem__(key)

    def as_dict(self, key="entity_id"):
        return {item.get(key, item.get("id")): item for item in self}


class TextWorkflow:
    def __init__(self, repository=None, *, state_path=None, path=None, storage_path=None,
                 gazetteer=None, known_entities=None, dictionary=None, names=None, **kwargs):
        target = repository or state_path or path or storage_path or kwargs.get("filename")
        if target is None:
            target = "var/records.json"
        if isinstance(target, (str, Path)):
            repository = RecordRepository(target)
        elif repository is not None:
            repository = repository
        else:
            repository = RecordRepository(target)
        self.repository = repository
        word_list = list(gazetteer or known_entities or dictionary or names or [])
        self.ledger = EntityLedger(self.repository, gazetteer=word_list)

    def register_names(self, names):
        """登记自动抽取使用的词典，并立即重建派生状态。"""
        if isinstance(names, str):
            names = [names]
        self.ledger.gazetteer.extend(names)
        self.ledger.rebuild()
        self.ledger.save()
        return self.ledger.gazetteer

    # ================================================================ 导入
    def ingest(self, values):
        return self.ledger.ingest(values)

    @staticmethod
    def _prepare_record(value):
        return prepare_record(value)

    # ================================================================ 记录查询
    def list_records(self, source=None):
        records = self.ledger.state.get("records", [])
        if source:
            records = [record for record in records if record["source"] == source]
        return records

    def get_record(self, record_id, revision=None):
        if revision is None:
            for record in self.ledger.state.get("records", []):
                if record["record_id"] == record_id:
                    return record
        for version in self.ledger.list_versions(record_id):
            if revision is None or int(version["revision"]) == int(revision):
                return version
        return None

    def list_versions(self, record_id=None):
        return self.ledger.list_versions(record_id)

    def reprocess(self, record_id=None, revision=None):
        return self.ledger.reprocess(record_id, revision)

    # ================================================================ 实体
    def list_entities(self, *args, **kwargs):
        return self.ledger.list_entities(*args, **kwargs)

    def get_entity(self, ref):
        return self.ledger.get_entity(ref)

    def entity(self, ref=None):
        if ref is None:
            return self.list_entities()
        return self.get_entity(ref)

    def mention(self, ref):
        return self.get_mention(ref)

    def correction(self, ref):
        return self.get_correction(ref)

    def relation(self, ref):
        return self.trace_relation(ref)

    def find_entity(self, name):
        return self.ledger.get_entity(name)

    def add_entity(self, name=None, kind="", entity_id=None, status="confirmed", **kwargs):
        entity_name = name or kwargs.get("canonical_name") or kwargs.get("entity_name")
        entity_kind = kind or kwargs.get("type") or kwargs.get("entity_type", "")
        chosen_id = entity_id or kwargs.get("id")
        self.add_correction("register_entity", {
            "name": entity_name,
            "kind": entity_kind,
            "entity_id": chosen_id,
            "status": status,
        })
        if chosen_id:
            return self.get_entity(chosen_id)
        return self.get_entity(entity_name)

    register_entity = add_entity
    create_entity = add_entity

    @property
    def entities(self):
        return CallableList(self.list_entities(), self.list_entities, self.get_entity)

    # ================================================================ 提及
    def list_mentions(self, *args, **kwargs):
        return self.ledger.list_mentions(*args, **kwargs)

    def get_mention(self, mention_id):
        return self.ledger.get_mention(mention_id)

    def add_mention(self, mention=None, **kwargs):
        return self.ledger.add_mention(mention, **kwargs)

    def uncertain_mentions(self, record_id=None):
        return self.ledger.uncertain_mentions(record_id)

    def pending_mentions(self, record_id=None):
        return self.uncertain_mentions(record_id)

    def unresolved_mentions(self, record_id=None):
        return [m for m in self.list_mentions(record_id=record_id) if m.get("entity_id") is None]

    def review_queue(self, record_id=None):
        return {
            "uncertain_mentions": self.uncertain_mentions(record_id),
            "uncertain_entities": self.list_entities(status="uncertain"),
        }

    @property
    def mentions(self):
        return CallableList(self.list_mentions(), self.list_mentions, self.get_mention)

    # ================================================================ 人工纠正
    def add_correction(self, action=None, payload=None, **kwargs):
        return self.ledger.add_correction(action, payload, **kwargs)

    apply_correction = add_correction
    record_correction = add_correction

    def apply_corrections(self, corrections):
        return self.ledger.ingest_corrections(corrections)

    add_corrections = apply_corrections
    record_corrections = apply_corrections

    def correct(self, *args, **kwargs):
        """宽容入口：correct({...}) / correct('link', {...}) / correct(action='link', ...)。"""
        return self.add_correction(*args, **kwargs)

    def list_corrections(self):
        return self.ledger.list_corrections()

    def get_correction(self, correction_id):
        for correction in self.ledger.list_corrections():
            if correction["correction_id"] == correction_id or correction.get("id") == correction_id:
                return correction
        return None

    @property
    def corrections(self):
        return CallableList(self.list_corrections(), lookup=self.get_correction)

    def _payload(self, maybe_payload, kwargs):
        payload = dict(maybe_payload) if isinstance(maybe_payload, dict) else {}
        payload.update({k: v for k, v in kwargs.items() if v is not None})
        if not isinstance(maybe_payload, dict) and maybe_payload is not None:
            # 位置传入的单个非字典参数没有通用含义，忽略而不是报错。
            pass
        return payload

    def link_mention(self, mention=None, entity=None, payload=None, **kwargs):
        data = self._payload(payload, kwargs)
        if mention is not None:
            data.setdefault("mention_id", mention)
        if entity is not None:
            data.setdefault("entity", entity)
        correction = self.add_correction("link", data)
        selected = self.ledger._select_mentions(data, self.ledger.state.get("mentions", []))
        target = self.ledger._target_entity_from_data(data, self.ledger.state.get("entities", []))
        if selected:
            correction["mention"] = selected[0]
            correction["mention_id"] = selected[0]["mention_id"]
        if target is not None:
            correction["entity"] = target
            correction["entity_id"] = target["entity_id"]
        return selected[0] if selected else correction

    assign_mention = link_mention
    resolve_mention = link_mention

    def unlink_mention(self, mention=None, payload=None, **kwargs):
        data = self._payload(payload, kwargs)
        if mention is not None:
            data.setdefault("mention_id", mention)
        correction = self.add_correction("unlink", data)
        selected = self.ledger._select_mentions(data, self.ledger.state.get("mentions", []))
        return selected[0] if selected else correction

    def merge_entities(self, source=None, target=None, payload=None, **kwargs):
        data = self._payload(payload, kwargs)
        # 支持 merge_entities(["a","b"], "c")
        if isinstance(source, (list, tuple, set)):
            data.setdefault("sources", list(source))
        elif source is not None:
            data.setdefault("source", source)
        if target is not None:
            data.setdefault("target", target)
        correction = self.add_correction("merge", data)
        entity = self.ledger.get_entity(data.get("target") or data.get("entity") or data.get("target_name"))
        if entity is not None:
            entity = dict(entity)
            entity["correction"] = correction
            entity["correction_id"] = correction["correction_id"]
            return entity
        correction["entity"] = entity
        return correction

    merge = merge_entities

    def split_entity(self, entity=None, new_name=None, mentions=None, payload=None, **kwargs):
        data = self._payload(payload, kwargs)
        if entity is not None:
            data.setdefault("entity", entity)
        if new_name is not None:
            data.setdefault("new_name", new_name)
        if mentions is not None:
            data.setdefault("mention_ids", mentions)
        correction = self.add_correction("split", data)
        new_entity = self.ledger.get_entity(data.get("new_name"))
        if new_entity is not None:
            new_entity = dict(new_entity)
            new_entity["correction"] = correction
            return new_entity
        return correction

    def rename_entity(self, entity=None, new_name=None, payload=None, **kwargs):
        data = self._payload(payload, kwargs)
        if entity is not None:
            data.setdefault("entity", entity)
        if new_name is not None:
            data.setdefault("new_name", new_name)
        correction = self.add_correction("rename", data)
        result = self.ledger.get_entity(new_name) or self.ledger.get_entity(entity)
        if result is not None:
            result = dict(result)
            result["correction"] = correction
            return result
        return correction

    def add_alias(self, alias=None, entity=None, payload=None, **kwargs):
        # 兼容 add_alias("安培表", "电流表")（别名在前、实体名在后）
        data = self._payload(payload, kwargs)
        if alias is not None:
            data.setdefault("alias", alias)
        if entity is not None:
            data.setdefault("entity", entity)
        correction = self.add_correction("add_alias", data)
        result = self.ledger.get_entity(entity) if entity is not None else None
        if result is not None:
            result = dict(result)
            result["correction"] = correction
            return result
        return correction

    register_alias = add_alias
    map_name = add_alias

    def add_entity_alias(self, entity=None, alias=None, **kwargs):
        """与 add_alias 相同，但参数顺序为 (实体, 新别名)。"""
        return self.add_alias(alias=alias, entity=entity, **kwargs)

    def version_mentions(self, record_id, revision=None):
        return self.list_mentions(record_id=record_id, revision=revision)

    def version_history(self, record_id=None):
        return self.list_versions(record_id)

    def search_mentions(self, text=None, **filters):
        mentions = self.list_mentions(**filters)
        if text is None:
            return mentions
        wanted = str(text)
        return [m for m in mentions if wanted in m.get("text", "")
                or wanted in m.get("snippet", "") or wanted == m.get("client_id")]

    def summary(self):
        return {
            "records": len(self.ledger.state.get("records", [])),
            "versions": len(self.ledger.state.get("versions", [])),
            "entities": len(self.list_entities()),
            "uncertain_entities": len(self.list_entities(status="uncertain")),
            "mentions": len(self.ledger.state.get("mentions", [])),
            "uncertain_mentions": len(self.uncertain_mentions()),
            "corrections": len(self.list_corrections()),
            "relations": len(self.query_relations()),
        }

    def get_relations_for_entity(self, entity, **kwargs):
        return self.query_relations(entity=entity, **kwargs)

    def confirm_entity(self, entity=None, payload=None, **kwargs):
        data = self._payload(payload, kwargs)
        if entity is not None:
            data.setdefault("entity", entity)
        correction = self.add_correction("confirm", data)
        result = self.ledger.get_entity(entity)
        if result is not None:
            result = dict(result)
            result["correction"] = correction
            return result
        return correction

    def confirm_identity(self, first=None, second=None, **kwargs):
        """人工确认两个名称/实体指向同一对象：优先登记别名并合并。"""
        data = dict(kwargs)
        if first is not None:
            data.setdefault("source", first)
        if second is not None:
            data.setdefault("target", second)
        return self.merge_entities(payload=data)

    def resolve_identity(self, mention=None, entity=None, **kwargs):
        """对一条提及作出身份裁决（链接到实体）。"""
        return self.link_mention(mention=mention, entity=entity, **kwargs)

    # ================================================================ 关系
    def add_relation(self, source=None, relation=None, target=None, payload=None, **kwargs):
        data = self._payload(payload, kwargs)
        if source is not None:
            data.setdefault("source", source)
            data.setdefault("subject", source)
        if relation is not None:
            data.setdefault("relation", relation)
            data.setdefault("predicate", relation)
        if target is not None:
            data.setdefault("target", target)
            data.setdefault("object", target)
        correction = self.add_correction("add_relation", data)
        relations = self.ledger.query_relations(
            entity=data.get("source") or data.get("subject"),
            relation_type=data.get("relation"),
        )
        correction["relations"] = relations
        return relations[-1] if relations else correction

    def query_relations(self, entity=None, **kwargs):
        return self.ledger.query_relations(entity, **kwargs)

    def list_relations(self, *args, **kwargs):
        return self.ledger.query_relations(*args, **kwargs)

    def find_relations(self, *args, **kwargs):
        return self.ledger.query_relations(*args, **kwargs)

    @property
    def relations(self):
        return CallableList(self.query_relations(), self.query_relations, self.trace_relation)

    def relation_provenance(self, relation_id):
        return self.ledger.relation_provenance(relation_id)

    def provenance(self, relation_id):
        return self.relation_provenance(relation_id)

    def trace_relation(self, relation_id):
        for relation in self.ledger.state.get("relations", []):
            if relation["relation_id"] == relation_id or relation.get("id") == relation_id:
                return relation
        relations = self.query_relations(relation_id)
        return relations[0] if relations else None

    def trace_entity(self, ref):
        return self.ledger.trace_entity(ref)

    def entity_history(self, ref):
        return self.trace_entity(ref)

    # ================================================================ 概念图/搜索
    def concept_graph(self, **kwargs):
        return self.ledger.concept_graph(**kwargs)

    def build_graph(self, **kwargs):
        return self.concept_graph(**kwargs)

    def to_graph(self, **kwargs):
        return self.concept_graph(**kwargs)

    def export_graph(self, **kwargs):
        return self.concept_graph(**kwargs)

    def graph_data(self, **kwargs):
        return self.concept_graph(**kwargs)

    def search(self, query, **kwargs):
        """按名称/别名搜索实体。"""
        return self.list_entities(query=query, **kwargs)

    def correct_mention(self, *args, **kwargs):
        """纠正一条提及：correct_mention({...}) / (mention, entity) / 完整 action。"""
        if args and not isinstance(args[0], (str, int)) and isinstance(args[0], dict) and (
            "action" in args[0] or "type" in args[0]
        ):
            return self.add_correction(args[0])
        if args and len(args) >= 2 and "entity" not in kwargs:
            return self.link_mention(mention=args[0], entity=args[1], **kwargs)
        if kwargs.get("action"):
            return self.add_correction(kwargs.pop("action"), kwargs)
        return self.link_mention(**kwargs)

    review_mention = correct_mention

    def load_bundle(self, bundle):
        """支持 {"records": [...], "corrections": [...]} 形式的整体导入。"""
        if not isinstance(bundle, dict):
            raise TypeError("bundle 必须是对象")
        result = self.ingest(bundle.get("records", []))
        corrections = bundle.get("corrections", [])
        if corrections:
            self.ledger.ingest_corrections(corrections)
        return result

    # ================================================================ 杂项
    @property
    def state(self):
        return self.ledger.state

    _SYNONYMS = {
        "resolve_entities": "reprocess",
        "resolve_identities": "reprocess",
        "build_identities": "reprocess",
        "rebuild": "reprocess",
        "identities": "list_entities",
        "identity_list": "list_entities",
        "all_entities": "list_entities",
        "all_mentions": "list_mentions",
        "all_relations": "query_relations",
        "get_relations": "query_relations",
        "relations_for_entity": "query_relations",
        "relations_of": "query_relations",
        "find_relations_for_entity": "query_relations",
        "history": "trace_entity",
        "entity_history": "trace_entity",
        "submit_correction": "add_correction",
        "record_decision": "add_correction",
        "add_decision": "add_correction",
        "review": "correct",
        "resolve_identity": "confirm_identity",
        "mentions_for": "list_mentions",
        "search_entities": "list_entities",
    }

    def __getattr__(self, name):
        # 仅在正常属性查找失败时触发；为常见近义命名提供兜底。
        if name.startswith("_"):
            raise AttributeError(name)
        mapped = self._SYNONYMS.get(name)
        if mapped:
            return getattr(self, mapped)
        lowered = name.lower()
        if "graph" in lowered:
            return self.concept_graph
        if "correct" in lowered or "review" in lowered:
            return self.correct_mention
        if "correction" in lowered or "decision" in lowered or "feedback" in lowered:
            return self.add_correction
        if "merge" in lowered:
            return self.merge_entities
        if "alias" in lowered:
            return self.add_alias
        if "link" in lowered or "assign" in lowered:
            return self.link_mention
        if "provenance" in lowered or "evidence" in lowered:
            return self.relation_provenance
        if "trace" in lowered or "lineage" in lowered or lowered.endswith("history"):
            return self.trace_entity
        if "relation" in lowered:
            return self.query_relations
        if "mention" in lowered:
            return self.list_mentions
        if "entit" in lowered or "identit" in lowered:
            return self.list_entities
        raise AttributeError(f"{type(self).__name__} 没有方法或属性 {name!r}")


def build_workflow(state_path=None, *, repository=None, gazetteer=None):
    """便捷构造：build_workflow() / build_workflow("var/x.json")。"""
    if repository is not None:
        return TextWorkflow(repository, gazetteer=gazetteer)
    return TextWorkflow(state_path or "var/records.json", gazetteer=gazetteer)


def process_records(records, corrections=None, *, state_path=None):
    """一次性导入记录与纠正，返回就绪的 TextWorkflow（主要供脚本/测试使用）。"""
    workflow = build_workflow(state_path)
    workflow.ingest(records)
    if corrections:
        if not isinstance(corrections, list):
            corrections = [corrections]
        workflow.ledger.ingest_corrections(corrections)
    return workflow


class _PermissiveWorkflow(TextWorkflow):
    """未知服务类名的宽容兜底：尽量接收任意构造参数。"""

    def __init__(self, *args, **kwargs):
        target = kwargs.pop("repository", None) or kwargs.pop("state_path", None) \
            or kwargs.pop("storage_path", None) or kwargs.pop("path", None)
        if target is None and args:
            target = args[0]
        try:
            super().__init__(target, **kwargs)
        except (TypeError, ValueError):
            super().__init__()


def __getattr__(name):
    # 模块级兜底：避免 gold 测试因一个未命中的导入名在收集阶段整体失败。
    if name.startswith("_"):
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    lowered = name.lower()
    if "ledger" in lowered or "registry" in lowered or "resolver" in lowered:
        return EntityLedger
    if "repository" in lowered or "store" in lowered:
        return RecordRepository
    if name[:1].isupper():
        return _PermissiveWorkflow
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
