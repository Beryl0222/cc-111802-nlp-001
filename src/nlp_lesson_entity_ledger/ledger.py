"""实体台账核心：实体身份、版本提及、人工纠正与关系溯源。

设计要点
--------
* ``versions`` 保存每个 ``(record_id, revision)`` 的不可变输入；
* ``corrections`` 是只追加的人工决定日志；
* ``entities / mentions / relations`` 都是由 “版本 + 纠正日志” 确定性
  重放得到的派生状态。因此重新处理任意旧版本都不会覆盖人工决定——
  重放时纠正总会在自动解析之后重新生效，且序号更大的纠正覆盖更早的。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .entities import (
    ALIAS,
    AMBIGUOUS,
    COLLISION,
    CONFIRMED,
    EXACT,
    HISTORICAL,
    MANUAL,
    MERGED,
    NONE,
    PENDING,
    PROVIDED,
    RESOLVED,
    RETRACTED,
    SIMILAR,
    UNCERTAIN,
    UNRESOLVED,
    Correction,
    normalize_action,
    kinds_compatible,
)
from .repository import RecordRepository, default_state
from .text import find_spans, name_key, name_similarity, quote_at, slug8

SIMILAR_THRESHOLD = 0.62
CANDIDATE_LIMIT = 6


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _looks_like_id(value: str) -> bool:
    token = str(value)
    if not token or any("一" <= ch <= "鿿" for ch in token):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_\-:.]*", token))


_MENTION_DECISION_KEYS = ("mention_id", "mention", "mid", "mention_ids", "ids", "mentions")
_ENTITY_DECISION_KEYS = ("entity", "entity_id", "entity_ref", "target", "entity_name", "target_name")


def _looks_like_link_decision(data: dict) -> bool:
    has_mention = any(data.get(key) is not None for key in _MENTION_DECISION_KEYS)
    has_entity = any(data.get(key) is not None for key in _ENTITY_DECISION_KEYS)
    return has_mention and has_entity


def _parse_time(value):
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def _first(d: dict, *keys, default=None):
    for key in keys:
        if key in d and d[key] not in (None, ""):
            return d[key]
    return default


class EntityLedger:
    def __init__(self, repository: "RecordRepository | str | Path | None" = None, *,
                 state: dict | None = None, gazetteer=None, path=None,
                 storage_path=None, state_path=None, known_entities=None,
                 dictionary=None, names=None, **kwargs):
        target = repository if repository is not None else (path or storage_path or state_path)
        if isinstance(target, (str, Path)):
            target = RecordRepository(target)
        self.repository = target
        if state is not None:
            self.state = state
        elif target is not None and hasattr(target, "read_state"):
            self.state = target.read_state()
        elif target is not None and hasattr(target, "read"):
            # 兼容只实现旧 read()/replace() 契约的仓储或测试替身。
            records = target.read()
            base = default_state()
            base["records"] = records if isinstance(records, list) else []
            self.state = base
        else:
            self.state = default_state()
        self.gazetteer = list(gazetteer or known_entities or dictionary or names or [])
        # 重放期间使用的运行时索引
        self._by_id: dict[str, dict] = {}
        self._name_index: dict[str, set[str]] = {}
        self._mention_index: dict[str, str] = {}
        self._retractions: list[dict] = []

    # ------------------------------------------------------------------ 持久化
    def save(self):
        if self.repository is None:
            return self.state
        if hasattr(self.repository, "write_state"):
            self.repository.write_state(self.state)
        elif hasattr(self.repository, "replace"):
            # 旧契约仓储：只持久化 records 视图。
            self.repository.replace(self.state.get("records", []))
        return self.state

    # ------------------------------------------------------------------ 版本输入
    def ingest(self, values):
        """与 TextWorkflow.ingest 相同的导入用例，便于直接使用台账核心。"""
        if not isinstance(values, list):
            raise TypeError("导入内容必须是记录列表")
        from .records import prepare_record
        from .text import annotate, normalize_text

        # 先解析校验全部记录，任何一条失败都不改动状态。
        prepared = [prepare_record(value) for value in values]
        current = {record["record_id"]: record for record in self.state.get("records", [])}
        for record in prepared:
            record["text"] = normalize_text(record["text"])
            record["analysis"] = annotate(record)
            self.upsert_version(record)
            previous = current.get(record["record_id"])
            if previous is None or record["revision"] >= previous["revision"]:
                current[record["record_id"]] = record
        result = sorted(current.values(), key=lambda row: row["record_id"])
        self.state["records"] = result
        self.rebuild()
        self.save()
        return {"received": len(prepared), "stored": len(result)}

    def ingest_corrections(self, values):
        if not isinstance(values, list):
            raise TypeError("纠正内容必须是列表")
        corrections = [self.add_correction(value if isinstance(value, dict) else {"action": value})
                       for value in values]
        return {"received": len(corrections), "stored": len(corrections)}

    def add_mention(self, mention=None, **kwargs):
        """增量登记一条提及。

        若提供了已存在的 ``record_id``/``revision``，会追加到该版本；
        否则放入 adhoc_mentions，同样参与身份解析与溯源。
        """
        spec = dict(mention) if isinstance(mention, dict) else ({"text": mention} if mention is not None else {})
        spec.update({k: v for k, v in kwargs.items() if v is not None})
        if not spec.get("text"):
            raise ValueError("提及缺少 text/名称")
        rid = spec.get("record_id")
        rev = int(spec.get("revision", 1) or 1)
        target_version = None
        if rid is not None:
            for version in self.state.get("versions", []):
                if version["record_id"] == rid and int(version["revision"]) == rev:
                    target_version = version
                    break
        if target_version is not None:
            target_version.setdefault("mentions", []).append(spec)
        else:
            spec["record_id"] = rid or "adhoc"
            spec["revision"] = rev
            self.state.setdefault("adhoc_mentions", []).append(spec)
        self.rebuild()
        self.save()
        client_id = spec.get("id") or spec.get("mention_id")
        if client_id:
            found = self.get_mention(str(client_id)) if hasattr(self, "get_mention") else None
            if found:
                return found
        # 返回同记录同文本的最后一条提及
        candidates = [m for m in self.state.get("mentions", [])
                      if m["record_id"] == spec.get("record_id", "adhoc") and m["text"] == spec["text"]]
        return candidates[-1] if candidates else None

    def get_mention(self, mention_id):
        for mention in self.state.get("mentions", []):
            if mention["mention_id"] == mention_id or mention.get("client_id") == mention_id:
                return mention
        return None

    def upsert_version(self, record: dict):
        versions = self.state.setdefault("versions", [])
        for index, version in enumerate(versions):
            if version["record_id"] == record["record_id"] and int(version["revision"]) == int(record["revision"]):
                versions[index] = record
                break
        else:
            versions.append(record)
        return record

    def list_versions(self, record_id=None):
        versions = list(self.state.get("versions", []))
        if record_id is not None:
            versions = [v for v in versions if v["record_id"] == record_id]
        return sorted(versions, key=lambda v: (str(v.get("observed_at", "")), v["record_id"], int(v["revision"])))

    def entities_for_version(self, record_id, revision=None):
        mentions = self.list_mentions(record_id=record_id, revision=revision)
        ids = {m.get("entity_id") or m.get("proposed_entity_id") for m in mentions}
        return [e for e in self.list_entities(include_merged=True) if e["entity_id"] in ids]

    def add_version(self, record):
        from .records import prepare_record
        from .text import annotate, normalize_text

        prepared = prepare_record(record)
        prepared["text"] = normalize_text(prepared["text"])
        prepared["analysis"] = annotate(prepared)
        self.upsert_version(prepared)
        current = {r["record_id"]: r for r in self.state.get("records", [])}
        previous = current.get(prepared["record_id"])
        if previous is None or prepared["revision"] >= previous["revision"]:
            current[prepared["record_id"]] = prepared
            self.state["records"] = sorted(current.values(), key=lambda row: row["record_id"])
        self.rebuild()
        self.save()
        return prepared

    def reprocess(self, record_id=None, revision=None):
        """重新运行自动派生；纠正日志保持不变并在 rebuild 时重新生效。"""
        from .text import annotate, normalize_text

        count = 0
        for version in self.state.get("versions", []):
            if record_id is not None and version["record_id"] != record_id:
                continue
            if revision is not None and int(version["revision"]) != int(revision):
                continue
            version["text"] = normalize_text(version.get("text", ""))
            version["analysis"] = annotate(version)
            count += 1
        self.rebuild()
        self.save()
        return {"reprocessed": count}

    # ================================================================== 重建
    def rebuild(self):
        versions = sorted(
            self.state.get("versions", []),
            key=lambda v: (_parse_time(v.get("observed_at")), v["record_id"], int(v["revision"])),
        )
        entities: list[dict] = []
        mentions: list[dict] = []
        self._by_id = {}
        self._name_index = {}
        self._mention_index = {}
        self._retractions = []
        self.state["_redirects"] = {}

        for version in versions:
            version_mentions = self._build_version_mentions(version)
            for mention in version_mentions:
                self._resolve_base(mention, entities)
                self._index_mention(mention)
                mentions.append(mention)

        # 允许通过 add_mention 增量登记的、不依附于完整导入记录的提及
        for spec in self.state.get("adhoc_mentions", []):
            pseudo_version = {
                "record_id": spec.get("record_id", "adhoc"),
                "revision": int(spec.get("revision", 1) or 1),
                "text": spec.get("record_text", ""),
                "observed_at": spec.get("observed_at", ""),
                "source": spec.get("source", "adhoc"),
                "context": spec.get("snippet", ""),
                "mentions": [spec],
            }
            for mention in self._build_version_mentions(pseudo_version):
                self._resolve_base(mention, entities)
                self._index_mention(mention)
                mentions.append(mention)

        # 人工纠正按序号重放（后决定覆盖先决定）
        corrections = sorted(self.state.get("corrections", []), key=lambda c: (int(c.get("seq", 0)), c.get("created_at", "")))
        manual_intents: list[dict] = []
        for correction in corrections:
            intents = self._replay_correction(correction, entities, mentions)
            manual_intents.extend(intents or [])

        # 合并重定向之后重建名称索引
        self._rebuild_name_index(entities)

        intents = list(manual_intents)
        for version in versions:
            intents.extend(self._version_relation_intents(version))

        relations = self._build_relations(intents, entities, mentions)

        self.state["entities"] = [e for e in entities]
        self.state["mentions"] = mentions
        self.state["relations"] = relations
        self.state["relation_intents"] = intents
        self._by_id = {e["entity_id"]: e for e in entities}
        return self.state

    # ------------------------------------------------------------------ 提及抽取
    def _build_version_mentions(self, version: dict):
        specs = list(version.get("mentions") or version.get("mention_specs") or [])
        if not specs:
            # 没有显式标注时，仅在提供了词典/gazetteer 时做最长匹配抽取，
            # 默认不猜测中文名称，避免凭空制造提及。
            gazetteer = list(self.gazetteer or [])
            gazetteer.extend(version.get("gazetteer") or version.get("dictionary") or [])
            text_for_match = version.get("text", "")
            if gazetteer:
                specs = [name for name in sorted({str(n) for n in gazetteer if n}, key=len, reverse=True)
                         if name in text_for_match]
        text = version.get("text", "")
        rid = version["record_id"]
        rev = int(version["revision"])

        normalized_specs = []
        occurrence_seen: dict[str, int] = {}
        for order, raw in enumerate(specs):
            spec = self._normalize_mention_spec(raw)
            if spec is None or not spec["text"]:
                continue
            surface = spec["text"]
            spans = find_spans(text, surface)
            occurrence = spec.get("occurrence")
            seen_index = occurrence_seen.get(surface, 0)
            occurrence_seen[surface] = seen_index + 1
            if isinstance(occurrence, int) and 0 <= occurrence < len(spans):
                start, end = spans[occurrence]
            elif spans:
                pick = min(seen_index, len(spans) - 1)
                start, end = spans[pick]
            elif isinstance(spec.get("start"), int) and spec["start"] >= 0:
                start = spec["start"]
                end = spec["end"] if isinstance(spec.get("end"), int) and spec["end"] > start else start + len(surface)
            else:
                start, end = -1, -1

            snippet = spec.get("snippet") or ""
            if not snippet and start >= 0:
                snippet = quote_at(text, start, end)
            elif not snippet:
                snippet = str(version.get("context", "") or "")

            ordinal = order
            mid = f"ment_{slug8(rid, rev, start if start >= 0 else 'x', order, surface)}"
            client_id = spec.get("client_id", "")
            normalized_specs.append({
                "mention_id": mid,
                # id 优先回显调用方给的 id；全局唯一键始终是 mention_id
                "id": client_id or mid,
                "global_id": mid,
                "record_id": rid,
                "revision": rev,
                "observed_at": version.get("observed_at", ""),
                "source": version.get("source", ""),
                "text": surface,
                "mention": surface,
                "name": surface,
                "surface": surface,
                "start": start,
                "end": end,
                "snippet": snippet,
                "quote": snippet,
                "kind": spec.get("kind", ""),
                "type": spec.get("kind", ""),
                "discriminator": spec.get("discriminator", ""),
                "occurrence": seen_index,
                "client_id": spec.get("client_id", ""),
                "provided_entity_ref": spec.get("entity_ref"),
                "order": ordinal,
            })

        normalized_specs.sort(key=lambda m: (m["start"] if m["start"] >= 0 else 10 ** 9, m["order"]))
        return normalized_specs

    @staticmethod
    def _normalize_mention_spec(raw):
        if isinstance(raw, str):
            return {"text": raw}
        if not isinstance(raw, dict):
            return None
        surface = _first(raw, "text", "mention", "surface", "surface_form", "name", "label", "value")
        if surface is None:
            return None
        client_id = _first(raw, "mention_id", "mid", "uuid", "client_id", "id")
        entity_ref = _first(raw, "entity_id", "entity", "entity_ref", "identity", "resolved_entity_id")
        # {"id": "e1", "name": "电流表"} 这种实体声明写法：id 同时作为
        # 预置实体 id 与可回查的 client id，两种调用约定都能找到它。
        has_offset_or_text = any(key in raw for key in ("text", "mention", "surface", "start", "start_offset", "begin", "end"))
        if entity_ref is None and raw.get("id") is not None and not has_offset_or_text and (
            "name" in raw or "canonical_name" in raw or "label" in raw
        ):
            entity_ref = raw.get("id")
        return {
            "text": str(surface),
            "client_id": "" if client_id is None else str(client_id),
            "entity_ref": entity_ref,
            "kind": str(_first(raw, "kind", "type", "entity_type", "category", default="") or ""),
            "start": raw.get("start", raw.get("start_offset", raw.get("begin"))),
            "end": raw.get("end", raw.get("end_offset")),
            "snippet": _first(raw, "snippet", "quote", "excerpt", "evidence", "context_text", default=""),
            "occurrence": raw.get("occurrence", raw.get("occurrence_index", raw.get("index"))),
            "discriminator": str(_first(raw, "discriminator", "setup", "scene", "group", "batch",
                                        "apparatus_set", "class_group", default="") or ""),
        }

    def _index_mention(self, mention: dict):
        self._mention_index[mention["mention_id"]] = mention["mention_id"]
        if mention.get("client_id"):
            self._mention_index[f"client:{mention['client_id']}"] = mention["mention_id"]

    # ------------------------------------------------------------------ 实体解析
    def _new_base_entity_id(self, entities):
        return f"ent-{len(entities) + 1}"

    def _create_entity(self, entities, name, kind="", status=UNCERTAIN, created_by="auto",
                       entity_id=None, discriminator="", note=""):
        eid = entity_id or self._new_base_entity_id(entities)
        mention_ids = []
        entity = {
            "entity_id": eid,
            "id": eid,
            "canonical_name": name,
            "name": name,
            "label": name,
            "kind": kind or "",
            "type": kind or "",
            "entity_type": kind or "",
            "status": status,
            "aliases": [],
            "former_ids": [],
            "mention_ids": mention_ids,
            # 近义键，与 mention_ids 指向同一个列表
            "mentions": mention_ids,
            "discriminators": [discriminator] if discriminator else [],
            "note": note,
            "created_by": created_by,
            "merged_into": "",
            "uncertain": status == UNCERTAIN,
            "confirmed": status == CONFIRMED,
        }
        entities.append(entity)
        self._by_id[eid] = entity
        key = name_key(name)
        if key:
            self._name_index.setdefault(key, set()).add(eid)
        return entity

    def _resolve_base(self, mention: dict, entities: list):
        surface = mention["text"]
        kind = mention.get("kind", "")
        disc = mention.get("discriminator", "")

        mention.update({
            "entity_id": None,
            "proposed_entity_id": None,
            "status": UNCERTAIN,
            "state": UNCERTAIN,
            "resolution": NONE,
            "confidence": 0.0,
            "candidate_ids": [],
            "candidates": [],
            "resolved": False,
            "needs_review": True,
            "is_uncertain": True,
            "pending": True,
            "ambiguous": False,
            "unresolved": True,
            "correction_id": "",
        })

        # 1) 标注里已经给出实体身份
        provided = mention.get("provided_entity_ref")
        if provided is not None:
            entity = self._ensure_ref_entity(provided, entities, surface, kind)
            if entity is not None:
                self._attach(mention, entity, PROVIDED, 1.0)
                return

        key = name_key(surface)
        exact_ids = set(self._name_index.get(key, set())) if key else set()
        exact_entities = [self._by_id[i] for i in exact_ids if i in self._by_id
                          and self._by_id[i].get("status") != MERGED]

        compatible = [e for e in exact_entities if self._kind_disc_ok(e, kind, disc)]

        # 2) 唯一精确名称/别名命中
        if len(exact_entities) == 1 and not compatible:
            # 同名但 kind/语境冲突：不能合并，走歧义分支
            mention["candidate_ids"] = [exact_entities[0]["entity_id"]]
            mention["candidates"] = [exact_entities[0]["entity_id"]]
            mention["resolution"] = COLLISION
            mention["ambiguous"] = True
            self._make_proposal(mention, entities, surface, kind, disc)
            return

        if len(compatible) == 1:
            entity = compatible[0]
            resolution = ALIAS if key in {name_key(a) for a in entity.get("aliases", [])} else EXACT
            self._attach(mention, entity, resolution, 1.0)
            if disc and disc not in entity["discriminators"]:
                entity["discriminators"].append(disc)
            return

        if len(compatible) > 1:
            ids = [e["entity_id"] for e in compatible]
            mention["candidate_ids"] = ids
            mention["candidates"] = list(ids)
            mention["resolution"] = COLLISION
            mention["ambiguous"] = True
            self._make_proposal(mention, entities, surface, kind, disc)
            return

        # 3) 没有精确命中：找简称/相似候选，但只作为候选，绝不自动合并
        candidates = self._similar_entities(surface, kind, entities)
        if candidates:
            ids = [e["entity_id"] for e, _ in candidates]
            mention["candidate_ids"] = ids
            mention["candidates"] = list(ids)
            mention["resolution"] = SIMILAR
            self._make_proposal(mention, entities, surface, kind, disc)
            return

        # 4) 全新名称：建立不确定的候选身份，等待人工确认/关联
        self._make_proposal(mention, entities, surface, kind, disc)
        mention["resolution"] = NONE

    def _make_proposal(self, mention, entities, surface, kind, disc):
        proposal = self._create_entity(entities, surface, kind, UNCERTAIN, "auto", discriminator=disc)
        mention["proposed_entity_id"] = proposal["entity_id"]
        # 候选身份保留到提及的反向登记，但 mention.entity_id 仍为 None，
        # 表示系统没有可靠地断言这一身份链接。
        proposal.setdefault("mention_ids", []).append(mention["mention_id"])
        return proposal

    @staticmethod
    def _kind_disc_ok(entity, kind, disc):
        if kind and entity.get("kind") and not kinds_compatible(kind, entity["kind"]):
            return False
        if disc and entity.get("discriminators") and disc not in entity["discriminators"]:
            return False
        return True

    def _similar_entities(self, surface, kind, entities):
        scored = []
        for entity in entities:
            if entity.get("status") == MERGED:
                continue
            names = [entity["canonical_name"], *entity.get("aliases", [])]
            score = max(name_similarity(surface, name) for name in names)
            if score >= SIMILAR_THRESHOLD:
                if kind and entity.get("kind") and not kinds_compatible(kind, entity["kind"]):
                    score *= 0.5
                scored.append((entity, score))
        scored.sort(key=lambda pair: -pair[1])
        return scored[:CANDIDATE_LIMIT]

    def _ensure_ref_entity(self, ref, entities, surface="", kind=""):
        """把外部传入的实体引用解析为实体；若是未知 id，则保留该 id 建卡。"""
        if isinstance(ref, dict):
            eid = _first(ref, "entity_id", "id", "entityId")
            name = _first(ref, "canonical_name", "name", "label", default=surface or str(eid))
            kind = kind or str(_first(ref, "kind", "type", "entity_type", default="") or "")
        else:
            eid = ref if isinstance(ref, str) else None
            name = surface or str(ref)
        if eid is None:
            return None
        eid = str(eid)
        if eid in self._by_id:
            return self._by_id[eid]
        if self._name_index.get(name_key(name)):
            ids = self._name_index[name_key(name)]
            if len(ids) == 1:
                return self._by_id[next(iter(ids))]
        return self._create_entity(entities, str(name), kind, CONFIRMED, "provided", entity_id=eid)

    def _attach(self, mention: dict, entity: dict, resolution: str, confidence: float, correction_id: str = ""):
        eid = self._resolve_redirect(entity["entity_id"])
        entity = self._by_id.get(eid, entity)
        mention["entity_id"] = eid
        mention["status"] = RESOLVED
        mention["state"] = RESOLVED
        mention["resolution"] = resolution
        mention["confidence"] = confidence
        mention["resolved"] = True
        mention["needs_review"] = False
        mention["is_uncertain"] = False
        mention["pending"] = False
        mention["unresolved"] = False
        if correction_id:
            mention["correction_id"] = correction_id
        ids = entity.setdefault("mention_ids", [])
        if mention["mention_id"] not in ids:
            ids.append(mention["mention_id"])
        # 若该提及原本只是某自动候选身份下的不确定提案，移走后清理空提案，
        # 避免“链接到新身份”之后还残留重名空壳实体。
        proposal_id = mention.get("proposed_entity_id")
        if correction_id and proposal_id and proposal_id != eid:
            proposal = self._by_id.get(proposal_id)
            if proposal is not None:
                proposal_ids = proposal.setdefault("mention_ids", [])
                if mention["mention_id"] in proposal_ids:
                    proposal_ids.remove(mention["mention_id"])
                if not proposal_ids and proposal.get("created_by") == "auto" \
                        and proposal.get("status") == UNCERTAIN:
                    proposal["status"] = MERGED
                    proposal["merged_into"] = eid
                    proposal["confirmed"] = False
                    proposal["uncertain"] = False
                    self.state.setdefault("_redirects", {})[proposal_id] = eid
        return mention

    def _resolve_redirect(self, eid):
        seen = set()
        while eid in self.state.get("_redirects", {}) and eid not in seen:
            seen.add(eid)
            eid = self.state["_redirects"][eid]
        return eid

    def _rebuild_name_index(self, entities):
        self._name_index = {}
        self._by_id = {}
        for entity in entities:
            self._by_id[entity["entity_id"]] = entity
            keys = {name_key(entity["canonical_name"])}
            keys.update(name_key(a) for a in entity.get("aliases", []))
            for key in keys:
                if key:
                    self._name_index.setdefault(key, set()).add(entity["entity_id"])

    # ------------------------------------------------------------------ 纠正
    def add_correction(self, action=None, payload=None, **kwargs):
        # 调用约定 1：add_correction({"action": "merge", ...})
        if isinstance(action, dict):
            data = dict(action)
            action = data.pop("action", data.pop("type", None))
        else:
            data = {}
        if isinstance(payload, dict):
            data.update(payload)
        data.update({k: v for k, v in kwargs.items() if v is not None})

        if action is None:
            action = data.pop("action", data.pop("type", None))
        if action is None and _looks_like_link_decision(data):
            # 宽容常见写法：add_correction(mention_id=..., entity_id=...)
            action = "link"

        canonical = normalize_action(action)

        corrections = self.state.setdefault("corrections", [])
        counters = self.state.setdefault("counters", {"correction": 0})
        counters["correction"] = int(counters.get("correction", len(corrections))) + 1
        seq = counters["correction"]

        correction_id = str(_first(data, "correction_id", "id", default=f"corr-{seq:04d}"))
        created_at = str(_first(data, "created_at", "at", "corrected_at", "decided_at", default=_now()))
        author = str(_first(data, "author", "reviewer", "by", default="reviewer"))
        note = str(_first(data, "note", "comment", default=""))

        # 元信息不重复放进业务 data
        for meta_key in ("correction_id", "id", "created_at", "at", "corrected_at", "decided_at",
                         "author", "reviewer", "by", "note", "comment", "action", "type"):
            data.pop(meta_key, None)

        correction = {
            "correction_id": correction_id,
            "id": correction_id,
            "seq": seq,
            "revision": seq,
            "decided_revision": seq,
            "action": canonical,
            "created_at": created_at,
            "at": created_at,
            "author": author,
            "note": note,
            "payload": data,
        }
        # 常用选择器提到顶层，方便读取
        for key in ("mention_id", "record_id", "revision", "entity_id", "source", "target",
                    "entity_name", "name", "alias", "relation_id"):
            if key in data:
                correction[key] = data[key]
        corrections.append(correction)
        self.rebuild()
        self.save()
        return correction

    def list_corrections(self):
        return sorted(self.state.get("corrections", []), key=lambda c: int(c.get("seq", 0)))

    def _replay_correction(self, correction: dict, entities: list, mentions: list):
        action = correction["action"]
        data = correction.get("payload", {})
        cid = correction["correction_id"]
        self._rebuild_name_index(entities)

        if action == "link":
            return self._corr_link(correction, data, entities, mentions)
        if action == "unlink":
            for mention in self._select_mentions(data, mentions):
                self._unlink_mention(mention, cid)
            return None
        if action == "confirm":
            for entity in self._select_entities(data, entities):
                entity["status"] = CONFIRMED
                entity["confirmed"] = True
                entity["uncertain"] = False
                # 人工确认身份后，挂在该候选身份上的提及一并转为已链接。
                for mid in list(entity.get("mention_ids", [])):
                    mention = self._mention_by_id(mentions, mid)
                    if mention is not None and not mention.get("resolved"):
                        self._attach(mention, entity, MANUAL, 1.0, cid)
            return None
        if action == "register_entity":
            eid = data.get("entity_id") or data.get("id")
            name = _first(data, "name", "canonical_name", "entity_name") or (str(eid) if eid else None)
            kind = str(_first(data, "kind", "entity_type", "type", default="") or "")
            existing = None
            if eid and str(eid) in self._by_id:
                existing = self._by_id[str(eid)]
            elif name:
                existing = self._find_entity_by_name(name, entities)
            if existing is None and name:
                status = data.get("status") if data.get("status") in (CONFIRMED, UNCERTAIN) else CONFIRMED
                self._create_entity(entities, str(name), kind, status, "manual",
                                    entity_id=str(eid) if eid else None)
            elif existing is not None and kind and not existing.get("kind"):
                existing["kind"] = kind
                existing["type"] = kind
            return None
        if action == "add_alias":
            entity = self._select_entity(data, entities)
            alias = _first(data, "alias", "alias_name", "name")
            if entity is None and _first(data, "entity", "entity_id", "target") is None:
                # add_alias("安培表", "电流表") 形式：alias 与 entity 名称位置参数
                alias, target_name = data.get("alias"), data.get("entity")
                entity = self._find_or_create_named(entities, target_name or data.get("canonical_name"))
            if entity is not None and alias:
                if alias not in entity["aliases"] and alias != entity["canonical_name"]:
                    entity["aliases"].append(str(alias))
                key = name_key(alias)
                if key:
                    self._name_index.setdefault(key, set()).add(entity["entity_id"])
                # 同名的自动候选身份应当被吸收，否则该名称会指向两个实体。
                for other in list(entities):
                    if other is entity or other.get("status") == MERGED:
                        continue
                    if name_key(other["canonical_name"]) == key:
                        self._absorb_entity(other, entity, mentions, cid)
                entity["status"] = CONFIRMED
                entity["confirmed"] = True
                entity["uncertain"] = False
            return None
        if action == "rename":
            entity = self._select_entity(data, entities)
            new_name = _first(data, "new_name", "new_canonical_name", "name", "canonical_name")
            if entity is not None and new_name:
                old = entity["canonical_name"]
                if data.get("keep_alias", True) and old and old != new_name and old not in entity["aliases"]:
                    entity["aliases"].append(old)
                entity["canonical_name"] = str(new_name)
                entity["name"] = str(new_name)
                entity["label"] = str(new_name)
                entity["status"] = CONFIRMED
                entity["confirmed"] = True
                entity["uncertain"] = False
            return None
        if action == "merge":
            return self._corr_merge(correction, data, entities, mentions)
        if action == "split":
            return self._corr_split(correction, data, entities, mentions)
        if action == "add_relation":
            # 作为手动意图返回，稍后统一建关系；先归一化端点字段。
            normalized = self._normalize_relation_spec(data) or {}
            intent = dict(data)
            intent.update(normalized)
            intent.setdefault("subject_ref", _first(data, "subject", "source", "from", "subject_ref"))
            intent.setdefault("object_ref", _first(data, "object", "target", "to", "object_ref"))
            intent.update({
                "origin": MANUAL,
                "correction_id": cid,
                "correction_seq": correction["seq"],
                "created_at": correction["created_at"],
                "author": correction.get("author", ""),
                "record_id": data.get("record_id"),
                "revision": data.get("revision"),
                "snippet": _first(data, "snippet", "quote", "excerpt", "evidence", default=""),
            })
            return [intent]
        if action == "remove_relation":
            self._retractions.append(data)
            return None
        return None

    def _corr_link(self, correction, data, entities, mentions):
        cid = correction["correction_id"]
        target = self._target_entity_from_data(data, entities)
        selected = self._select_mentions(data, mentions)
        for mention in selected:
            if target is None:
                continue
            self._attach(mention, target, MANUAL, 1.0, cid)
            target["status"] = CONFIRMED
            target["confirmed"] = True
            target["uncertain"] = False
        return None

    def _target_entity_from_data(self, data, entities):
        target_ref = _first(data, "entity", "target", "target_id", "into",
                            "entity_id", "entity_ref", "to")
        name = _first(data, "entity_name", "target_name", "name", "canonical_name")
        kind = str(_first(data, "kind", "entity_type", "type", default="") or "")
        if isinstance(target_ref, dict):
            return self._ensure_ref_entity(target_ref, entities, name or target_ref.get("name", ""), kind)
        # 显式名称优先
        if name:
            found = self._find_entity_by_name(name, entities)
            if found is not None:
                return found
        if target_ref is not None:
            ref = str(target_ref)
            if ref in self._by_id:
                return self._by_id[ref]
            found = self._find_entity_by_name(ref, entities)
            if found is not None:
                return found
            if _looks_like_id(ref) and not name:
                # 像预设主键：保留该 id 建卡
                return self._create_entity(entities, ref, kind, CONFIRMED, "correction", entity_id=ref)
            return self._create_entity(entities, name or ref, kind or "thing", CONFIRMED, "correction")
        if name:
            return self._create_entity(entities, str(name), kind or "thing", CONFIRMED, "correction")
        return None

    def _find_or_create_named(self, entities, name, kind=""):
        found = self._find_entity_by_name(name, entities)
        if found is not None:
            return found
        return self._create_entity(entities, str(name), kind, CONFIRMED, "correction")

    def _find_entity_by_name(self, name, entities):
        if name is None:
            return None
        text = str(name)
        if text in self._by_id:
            return self._by_id[text]
        key = name_key(text)
        ids = self._name_index.get(key)
        if ids and len(ids) == 1:
            return self._by_id[next(iter(ids))]
        # 退回线性扫描（纠正重放早期索引可能尚未覆盖最新实体）
        matches = [e for e in entities if e.get("status") != MERGED and (
            e["canonical_name"] == text or text in e.get("aliases", []))]
        if len(matches) == 1:
            return matches[0]
        return matches[0] if matches else None

    def _select_entity(self, data, entities):
        ref = _first(data, "entity", "entity_id", "source", "target", "ref")
        if isinstance(ref, dict):
            return self._ensure_ref_entity(ref, entities)
        if ref is not None:
            ref = str(ref)
            if ref in self._by_id:
                return self._by_id[ref]
            found = self._find_entity_by_name(ref, entities)
            if found:
                return found
        name = _first(data, "entity_name", "name", "canonical_name")
        if name:
            return self._find_entity_by_name(name, entities)
        return None

    def _select_entities(self, data, entities):
        refs = data.get("entities") or data.get("entity_ids")
        result = []
        if refs:
            for ref in refs:
                entity = self._select_entity({"entity": ref} if not isinstance(ref, dict) else ref, entities)
                if entity:
                    result.append(entity)
        else:
            entity = self._select_entity(data, entities)
            if entity:
                result.append(entity)
        return result

    @staticmethod
    def _mention_by_id(mentions, mid):
        for mention in mentions:
            if mention["mention_id"] == mid or mention.get("client_id") == mid:
                return mention
        return None

    def _select_mentions(self, data, mentions):
        # 显式 mention_id / 列表
        direct = _first(data, "mention_ids", "ids", "mentions")
        candidates = list(mentions)
        if direct is not None:
            wanted = direct if isinstance(direct, (list, tuple, set)) else [direct]
            wanted_keys = {str(w) for w in wanted}
            picked = []
            for m in mentions:
                if m["mention_id"] in wanted_keys or m.get("client_id") in wanted_keys:
                    picked.append(m)
            if picked:
                return picked
            # 没匹配上时继续把 wanted 当作选择器线索
        mention_id = _first(data, "mention_id", "mention", "mid")
        record_id = data.get("record_id")
        revision = data.get("revision")
        text = _first(data, "text", "mention_text", "name", "surface")

        picked = candidates
        if mention_id is not None:
            key = str(mention_id)
            picked = [m for m in picked if m["mention_id"] == key or m.get("client_id") == key]
        if record_id is not None:
            picked = [m for m in picked if m["record_id"] == record_id]
        if revision is not None:
            picked = [m for m in picked if int(m["revision"]) == int(revision)]
        if text is not None:
            wanted = name_key(text)
            picked = [m for m in picked if name_key(m["text"]) == wanted or text == m["text"]]
        occurrence = data.get("occurrence")
        if isinstance(occurrence, int) and mention_id is None and text is not None:
            same = [m for m in picked if name_key(m["text"]) == name_key(text)]
            if 0 <= occurrence < len(same):
                return [same[occurrence]]
        return picked

    @staticmethod
    def _unlink_mention(mention, cid):
        mention["entity_id"] = None
        mention["status"] = UNCERTAIN
        mention["state"] = UNCERTAIN
        mention["resolution"] = "manual_unlinked"
        mention["resolved"] = False
        mention["needs_review"] = True
        mention["is_uncertain"] = True
        mention["pending"] = True
        mention["unresolved"] = True
        mention["correction_id"] = cid

    def _absorb_entity(self, source, target, mentions, cid):
        """把 source 的名称与提及并入 target，并把 source 标记为 merged。"""
        if source is None or target is None or source["entity_id"] == target["entity_id"]:
            return
        for alias in [source["canonical_name"], *source.get("aliases", [])]:
            if alias and alias not in target["aliases"] and alias != target["canonical_name"]:
                target["aliases"].append(alias)
        if source["entity_id"] not in target.setdefault("former_ids", []):
            target["former_ids"].append(source["entity_id"])
        moved = set()
        for mention in mentions:
            if mention.get("entity_id") == source["entity_id"]:
                self._attach(mention, target, MANUAL, 1.0, cid)
                moved.add(mention["mention_id"])
        for mid in list(source.get("mention_ids", [])):
            mention = self._mention_by_id(mentions, mid)
            if mention is not None and mention["mention_id"] not in moved:
                self._attach(mention, target, MANUAL, 1.0, cid)
                moved.add(mention["mention_id"])
        source["status"] = MERGED
        source["merged_into"] = target["entity_id"]
        source["confirmed"] = False
        source["uncertain"] = False
        self.state.setdefault("_redirects", {})[source["entity_id"]] = target["entity_id"]

    def _corr_merge(self, correction, data, entities, mentions):
        cid = correction["correction_id"]
        source_refs = data.get("sources") or data.get("source_ids") or [data.get("source", data.get("from_entity"))]
        target = self._target_entity_from_data(data, entities)
        if target is None:
            return None
        for ref in source_refs:
            if ref is None:
                continue
            source = self._select_entity({"entity": ref}, entities)
            if source is None:
                # 源名称可能只作为关系端点出现、尚未建卡；仍把它登记为
                # target 的别名，保证后续关系端点解析能合并到同一身份。
                source_name = str(ref)
                if not _looks_like_id(source_name) and source_name != target["canonical_name"]:
                    if source_name not in target["aliases"]:
                        target["aliases"].append(source_name)
                    key = name_key(source_name)
                    if key:
                        self._name_index.setdefault(key, set()).add(target["entity_id"])
                continue
            self._absorb_entity(source, target, mentions, cid)
        # target 自身候选提及在人工合并后同样转为已链接
        for mid in list(target.get("mention_ids", [])):
            mention = self._mention_by_id(mentions, mid)
            if mention is not None and not mention.get("resolved"):
                self._attach(mention, target, MANUAL, 1.0, cid)
        target["status"] = CONFIRMED
        target["confirmed"] = True
        target["uncertain"] = False
        return None

    def _corr_split(self, correction, data, entities, mentions):
        cid = correction["correction_id"]
        source = self._select_entity(data, entities)
        new_name = _first(data, "new_name", "name", "target_name", "entity_name")
        if not new_name:
            return None
        new_entity = self._create_entity(
            entities, str(new_name), str(data.get("kind", (source or {}).get("kind", "")) or ""),
            CONFIRMED, "correction", entity_id=data.get("new_entity_id"))
        selector = dict(data)
        selected = self._select_mentions(selector, mentions)
        for mention in selected:
            self._attach(mention, new_entity, MANUAL, 1.0, cid)
        new_entity["status"] = CONFIRMED
        return None

    # ------------------------------------------------------------------ 关系
    def _version_relation_intents(self, version):
        raw_list = list(version.get("relations") or version.get("relation_specs") or [])
        intents = []
        rid = version["record_id"]
        rev = int(version["revision"])
        text = version.get("text", "")
        for raw in raw_list:
            intent = self._normalize_relation_spec(raw)
            if intent is None:
                continue
            subj = intent["subject_ref"]
            obj = intent["object_ref"]
            snippet = intent.get("snippet") or ""
            if not snippet:
                snippet = self._span_snippet_for_endpoints(text, subj, obj)
            intents.append({
                "subject_ref": subj,
                "object_ref": obj,
                "relation": intent["relation"],
                "snippet": snippet,
                "record_id": intent.get("record_id") or rid,
                "revision": intent.get("revision") or rev,
                "observed_at": version.get("observed_at", ""),
                "source": version.get("source", ""),
                "origin": "extracted",
                "correction_id": None,
            })
        return intents

    @staticmethod
    def _normalize_relation_spec(raw):
        if isinstance(raw, (list, tuple)) and len(raw) >= 3:
            return {"subject_ref": raw[0], "relation": str(raw[1]), "object_ref": raw[2],
                    "snippet": raw[3] if len(raw) > 3 else ""}
        if not isinstance(raw, dict):
            return None
        subj = _first(raw, "subject", "source", "from", "from_entity", "head", "arg1", "x", "subject_ref")
        obj = _first(raw, "object", "target", "to", "to_entity", "tail", "arg2", "y", "object_ref")
        relation = _first(raw, "relation", "predicate", "type", "rel", "verb", "name")
        if subj is None or obj is None or relation is None:
            return None
        return {
            "subject_ref": subj,
            "object_ref": obj,
            "relation": str(relation),
            "snippet": _first(raw, "snippet", "quote", "excerpt", "evidence", default=""),
            "record_id": raw.get("record_id"),
            "revision": raw.get("revision"),
        }

    def _span_snippet_for_endpoints(self, text, subj, obj):
        names = []
        for ref in (subj, obj):
            if isinstance(ref, str):
                names.append(ref)
            elif isinstance(ref, dict):
                name = _first(ref, "text", "name", "canonical_name")
                if name:
                    names.append(str(name))
        spans = []
        for name in names:
            found = find_spans(text, name)
            if found:
                spans.append(found[0])
        if not spans:
            return ""
        start = min(s[0] for s in spans)
        end = max(s[1] for s in spans)
        return quote_at(text, start, end)

    def _resolve_endpoint(self, ref, intent, mentions):
        """返回 (entity_id, display_name, uncertain)。"""
        # 字典引用
        if isinstance(ref, dict):
            eid = _first(ref, "entity_id")
            if eid and str(eid) in self._by_id:
                return str(eid), self._by_id[str(eid)]["canonical_name"], self._by_id[str(eid)]["status"] == UNCERTAIN
            # 可能是 {"mention_id": ...} 或 {"id": "<mention/client id>"}
            mention_token = _first(ref, "mention_id", "mid", "id")
            if mention_token is not None:
                mention = self._lookup_mention(str(mention_token), intent, mentions)
                if mention is not None:
                    if mention.get("entity_id"):
                        return mention["entity_id"], mention["text"], False
                    proposed = mention.get("proposed_entity_id")
                    if proposed and proposed in self._by_id:
                        return proposed, mention["text"], True
            name = _first(ref, "name", "text", "canonical_name")
            if name:
                return self._endpoint_by_name(str(name))
            raw_id = _first(ref, "id")
            if raw_id:  # 未知实体 id：建卡保留
                build_entities = getattr(self, "_build_entities", None)
                if build_entities is None:
                    build_entities = self.state.setdefault("entities", [])
                entity = self._ensure_ref_entity(ref, build_entities, str(name or raw_id))
                return entity["entity_id"], entity["canonical_name"], True
            return None, "", True

        if ref is None:
            return None, "", True
        token = str(ref)

        # mention id / client id（优先限定在同一版本）
        mention = self._lookup_mention(token, intent, mentions)
        if mention is not None:
            if mention.get("entity_id"):
                return mention["entity_id"], mention["text"], False
            proposed = mention.get("proposed_entity_id")
            if proposed and proposed in self._by_id:
                return proposed, mention["text"], True

        if token in self._by_id:
            entity = self._by_id[token]
            return token, entity["canonical_name"], entity["status"] == UNCERTAIN

        return self._endpoint_by_name(token)

    def _lookup_mention(self, token, intent, mentions):
        rid = (intent or {}).get("record_id")
        rev = (intent or {}).get("revision")
        scoped = [m for m in mentions if (rid is None or m["record_id"] == rid)
                  and (rev is None or int(m["revision"]) == int(rev))]
        for pool in (scoped, mentions):
            for m in pool:
                if m["mention_id"] == token or m.get("client_id") == token:
                    return m
        return None

    def _endpoint_by_name(self, name):
        key = name_key(name)
        ids = list(self._name_index.get(key, set())) if key else []
        candidates = [self._by_id[i] for i in ids
                      if i in self._by_id and self._by_id[i].get("status") != MERGED]
        if len(candidates) == 1:
            e = candidates[0]
            return e["entity_id"], e["canonical_name"], e["status"] == UNCERTAIN
        if len(candidates) > 1:
            def rank(entity):
                exact = name_key(entity["canonical_name"]) == key
                confirmed = entity.get("status") == CONFIRMED
                return (confirmed, exact, entity["entity_id"])

            chosen = sorted(candidates, key=rank, reverse=True)[0]
            ambiguous = sum(1 for e in candidates if e.get("status") == CONFIRMED) != 1
            return chosen["entity_id"], chosen["canonical_name"], ambiguous
        # 找不到就建一张不确定身份卡，保证关系仍可查询、可事后纠正
        build_entities = getattr(self, "_build_entities", None)
        if build_entities is None:
            build_entities = self.state.setdefault("entities", [])
        entity = self._create_entity(build_entities, name, "", UNCERTAIN, "relation-endpoint")
        self._rebuild_name_index(build_entities)
        return entity["entity_id"], name, True

    def _build_relations(self, intents, entities, mentions):
        self._build_entities = entities
        groups: dict[tuple, dict] = {}
        order = []
        retracted_keys = set()
        latest_rev = {}
        for record in self.state.get("records", []):
            latest_rev[record["record_id"]] = int(record["revision"])

        for intent in intents:
            source_id, source_name, source_uncertain = self._resolve_endpoint(intent.get("subject_ref"), intent, mentions)
            target_id, target_name, target_uncertain = self._resolve_endpoint(intent.get("object_ref"), intent, mentions)
            relation_name = str(intent.get("relation", "")).strip()
            key = (source_id or f"name:{source_name}", name_key(relation_name), target_id or f"name:{target_name}")

            effect = self._retraction_effect(intent)
            if effect == "drop":
                continue
            if effect == "retract":
                retracted_keys.add(key)

            manual = intent.get("origin") == MANUAL
            evidence = {
                "record_id": intent.get("record_id"),
                "revision": intent.get("revision"),
                "version": intent.get("revision"),
                "observed_at": intent.get("observed_at", ""),
                "source": intent.get("source", ""),
                "snippet": intent.get("snippet", ""),
                "quote": intent.get("snippet", ""),
                "origin": intent.get("origin", "extracted"),
                "correction_id": intent.get("correction_id"),
                "correction_seq": intent.get("correction_seq"),
                "at": intent.get("created_at") or (intent.get("observed_at", "") if not manual else intent.get("created_at")),
                "author": intent.get("author", ""),
                "mention_ids": self._evidence_mention_ids(intent, mentions),
            }

            if key not in groups:
                rid = f"rel_{slug8(*key)}"
                groups[key] = {
                    "relation_id": rid,
                    "id": rid,
                    "relation": relation_name,
                    "predicate": relation_name,
                    "type": relation_name,
                    "name": relation_name,
                    "source": source_id,
                    "target": target_id,
                    "source_id": source_id,
                    "target_id": target_id,
                    "source_entity_id": source_id,
                    "target_entity_id": target_id,
                    "from_id": source_id,
                    "to_id": target_id,
                    "subject_id": source_id,
                    "object_id": target_id,
                    "subject": source_name,
                    "object": target_name,
                    "subject_name": source_name,
                    "object_name": target_name,
                    "source_ref": source_id or source_name,
                    "target_ref": target_id or target_name,
                    "source_uncertain": source_uncertain,
                    "target_uncertain": target_uncertain,
                    "status": UNCERTAIN,
                    "evidence": [],
                    "provenance": [],
                    "record_ids": [],
                    "revisions": [],
                    "origins": [],
                    "current": False,
                    "historical": False,
                }
                order.append(key)
            group = groups[key]
            group["evidence"].append(evidence)
            group["provenance"].append(evidence)
            if evidence["record_id"] and evidence["record_id"] not in group["record_ids"]:
                group["record_ids"].append(evidence["record_id"])
            if evidence["revision"] is not None and evidence["revision"] not in group["revisions"]:
                group["revisions"].append(evidence["revision"])
            group["origins"].append(evidence["origin"])

        relations = []
        for key in order:
            group = groups[key]
            manual_evidence = [e for e in group["evidence"] if e["origin"] == MANUAL]
            endpoint_uncertain = group["source_uncertain"] or group["target_uncertain"]
            group["status"] = CONFIRMED if manual_evidence or not endpoint_uncertain else UNCERTAIN
            if key in retracted_keys:
                group["status"] = RETRACTED
            # 是否仍出现在该记录的最新修订中
            current = bool(manual_evidence)
            for ev in group["evidence"]:
                rid, rev = ev.get("record_id"), ev.get("revision")
                if rid and latest_rev.get(rid) == int(rev):
                    current = True
            group["current"] = current
            group["present_in_latest_version"] = current
            group["historical"] = any(
                ev.get("record_id") and latest_rev.get(ev["record_id"]) is not None
                and int(ev.get("revision")) < latest_rev[ev["record_id"]]
                for ev in group["evidence"]
            ) and not current
            relations.append(group)

        self.state["relations"] = relations
        return relations

    def _evidence_mention_ids(self, intent, mentions):
        ids = []
        for ref in (intent.get("subject_ref"), intent.get("object_ref")):
            token = None
            if isinstance(ref, str):
                token = ref
            elif isinstance(ref, dict):
                token = _first(ref, "mention_id", "id", "mid")
            if token:
                m = self._lookup_mention(str(token), intent, mentions)
                if m:
                    ids.append(m["mention_id"])
        return ids

    def _retraction_effect(self, intent):
        """判断一条关系意图是否命中撤销规则。

        * 带 record_id/revision 的撤销只 “drop” 对应版本的证据；
        * 不带版本范围的撤销 “retract” 整条逻辑关系（保留审计，默认查询隐藏）。
        """
        for rule in self._retractions:
            if rule.get("relation_id") and rule["relation_id"] == intent.get("relation_id"):
                return "retract"
            subj = _first(rule, "subject", "source", "from")
            obj = _first(rule, "object", "target", "to")
            rel = _first(rule, "relation", "predicate", "type")
            if rel and name_key(str(rel)) != name_key(str(intent.get("relation", ""))):
                continue
            if subj is not None and name_key(str(subj)) != name_key(str(self._ref_name(intent.get("subject_ref")))):
                continue
            if obj is not None and name_key(str(obj)) != name_key(str(self._ref_name(intent.get("object_ref")))):
                continue
            scoped = rule.get("record_id") is not None or rule.get("revision") is not None
            if rule.get("record_id") and rule["record_id"] != intent.get("record_id"):
                continue
            if rule.get("revision") is not None:
                intent_revision = intent.get("revision")
                if intent_revision is None or int(rule["revision"]) != int(intent_revision):
                    continue
            if not any(k in rule for k in ("subject", "source", "object", "target", "relation", "relation_id")):
                continue
            return "drop" if scoped else "retract"
        return None

    def _is_retracted(self, intent):  # 兼容旧命名
        return self._retraction_effect(intent) is not None

    @staticmethod
    def _ref_name(ref):
        if isinstance(ref, dict):
            return _first(ref, "name", "text", "canonical_name", "id", default="")
        return "" if ref is None else str(ref)

    # ------------------------------------------------------------------ 查询
    def _ensure_built(self):
        if "entities" not in self.state or "mentions" not in self.state:
            self.rebuild()
        else:
            self._by_id = {e["entity_id"]: e for e in self.state.get("entities", [])}
            self._name_index = {}
            for e in self.state.get("entities", []):
                for key in {name_key(e["canonical_name"]), *(name_key(a) for a in e.get("aliases", []))}:
                    if key:
                        self._name_index.setdefault(key, set()).add(e["entity_id"])

    def list_entities(self, status=None, kind=None, query=None, include_merged=False):
        self._ensure_built()
        result = list(self.state.get("entities", []))
        if not include_merged:
            result = [e for e in result if e.get("status") != MERGED]
        if status:
            wanted = {status} if isinstance(status, str) else set(status)
            alias_map = {"pending": UNCERTAIN, "ambiguous": UNCERTAIN, "unresolved": UNCERTAIN}
            wanted = {alias_map.get(w, w) for w in wanted}
            result = [e for e in result if e.get("status") in wanted]
        if kind:
            result = [e for e in result if kinds_compatible(kind, e.get("kind", "")) or e.get("kind") == kind]
        if query:
            key = name_key(query)
            result = [e for e in result if key == name_key(e["canonical_name"])
                      or key in {name_key(a) for a in e.get("aliases", [])}
                      or key in {name_key(i) for i in e.get("former_ids", [])}]
        return result

    def get_entity(self, ref):
        self._ensure_built()
        if ref is None:
            return None
        text = str(ref)
        if text in self._by_id:
            return self._by_id[text]
        # 合并重定向
        redirected = self.state.get("_redirects", {}).get(text)
        if redirected:
            return self._by_id.get(redirected)
        return self._find_entity_by_name(text, list(self.state.get("entities", [])))

    def list_mentions(self, record_id=None, revision=None, status=None, entity=None):
        result = list(self.state.get("mentions", []))
        if record_id is not None:
            result = [m for m in result if m["record_id"] == record_id]
        if revision is not None:
            result = [m for m in result if int(m["revision"]) == int(revision)]
        if status:
            if status in (UNCERTAIN, PENDING, AMBIGUOUS, UNRESOLVED):
                result = [m for m in result if m.get("status") == UNCERTAIN or m.get("state") in (PENDING, AMBIGUOUS)]
            else:
                result = [m for m in result if m.get("status") == status]
        if entity is not None:
            target = self.get_entity(entity)
            eid = target["entity_id"] if target else str(entity)
            result = [m for m in result if m.get("entity_id") == eid or m.get("proposed_entity_id") == eid]
        return result

    def uncertain_mentions(self, record_id=None):
        return [m for m in self.list_mentions(record_id=record_id) if not m.get("resolved")]

    def query_relations(self, entity=None, *, relation_type=None, type=None, include_historical=True,
                        current_only=False, record_id=None, status=None, include_retracted=False,
                        source=None, target=None, subject=None, object=None):
        self._ensure_built()
        result = list(self.state.get("relations", []))
        if not include_retracted:
            result = [r for r in result if r.get("status") != RETRACTED]
        rel_name = relation_type or type
        if rel_name:
            wanted = name_key(rel_name)
            result = [r for r in result if name_key(r["relation"]) == wanted]
        if entity is not None:
            targets = self._entity_and_redirect_ids(entity)
            result = [r for r in result if r.get("source") in targets or r.get("target") in targets]
        source_ref = source if source is not None else subject
        target_ref = target if target is not None else object
        if source_ref is not None:
            ids = self._entity_and_redirect_ids(source_ref)
            result = [r for r in result if r.get("source") in ids]
        if target_ref is not None:
            ids = self._entity_and_redirect_ids(target_ref)
            result = [r for r in result if r.get("target") in ids]
        if record_id is not None:
            result = [r for r in result if record_id in r.get("record_ids", [])
                      or any(e.get("record_id") == record_id for e in r.get("evidence", []))]
        if current_only:
            result = [r for r in result if r.get("current")]
        elif not include_historical:
            result = [r for r in result if r.get("current")]
        if status:
            result = [r for r in result if r.get("status") == status]
        return result

    def _entity_and_redirect_ids(self, entity):
        target = self.get_entity(entity) if not isinstance(entity, (list, tuple, set)) else None
        if isinstance(entity, (list, tuple, set)):
            ids = set()
            for item in entity:
                ids |= self._entity_and_redirect_ids(item)
            return ids
        if target is None:
            return {str(entity)}
        ids = {target["entity_id"]}
        # 包含被合并进该实体的旧 id
        for e in self.state.get("entities", []):
            if e.get("merged_into") == target["entity_id"]:
                ids.add(e["entity_id"])
            if target["entity_id"] in e.get("former_ids", []):
                ids.add(e["entity_id"])
        return ids

    def relation_provenance(self, relation_id):
        for relation in self.state.get("relations", []):
            if relation["relation_id"] == relation_id or relation.get("id") == relation_id:
                return relation.get("evidence", [])
        # 也许调用方直接传了实体/名称，返回匹配关系的证据
        relations = self.query_relations(relation_id)
        if len(relations) == 1:
            return relations[0]["evidence"]
        return []

    def concept_graph(self, *, include_uncertain=True, current_only=False):
        """导出下游概念关系图需要的 nodes/edges 结构。"""
        self._ensure_built()
        entities = [e for e in self.state.get("entities", []) if e.get("status") != MERGED]
        nodes = [{
            "id": e["entity_id"],
            "entity_id": e["entity_id"],
            "label": e["canonical_name"],
            "name": e["canonical_name"],
            "kind": e.get("kind", ""),
            "type": e.get("kind", ""),
            "status": e.get("status", UNCERTAIN),
            "aliases": e.get("aliases", []),
        } for e in entities]
        relations = self.query_relations(current_only=current_only)
        edges = []
        for relation in relations:
            if not include_uncertain and relation.get("status") == UNCERTAIN:
                continue
            edges.append({
                "id": relation["relation_id"],
                "relation_id": relation["relation_id"],
                "source": relation.get("source"),
                "target": relation.get("target"),
                "source_id": relation.get("source"),
                "target_id": relation.get("target"),
                "label": relation["relation"],
                "relation": relation["relation"],
                "type": relation["relation"],
                "status": relation.get("status"),
                "current": relation.get("current"),
                "evidence_count": len(relation.get("evidence", [])),
                "evidence": relation.get("evidence", []),
            })
        return {"nodes": nodes, "edges": edges,
                # 近义键，兼容不同图工具的命名
                "vertices": nodes, "links": edges}

    def trace_entity(self, ref):
        entity = self.get_entity(ref)
        eid = entity["entity_id"] if entity else str(ref)
        mentions = [m for m in self.state.get("mentions", [])
                    if m.get("entity_id") == eid or m.get("proposed_entity_id") == eid]
        relation_ids = set()
        relations = []
        for relation in self.state.get("relations", []):
            if relation.get("source") == eid or relation.get("target") == eid:
                relations.append(relation)
                relation_ids.add(relation["relation_id"])
        corrections = [c for c in self.state.get("corrections", [])
                       if eid in json_safe_ids(c) or ref_matches_correction(c, entity)]
        timeline = []
        for m in mentions:
            timeline.append({"kind": "mention", "at": m.get("observed_at", ""), "record_id": m["record_id"],
                             "revision": m["revision"], "snippet": m.get("snippet", ""), "mention": m["text"],
                             "status": m["status"]})
        for c in corrections:
            timeline.append({"kind": "correction", "at": c.get("created_at", ""), "correction_id": c["correction_id"],
                             "seq": c["seq"], "action": c["action"], "author": c.get("author", "")})
        timeline.sort(key=lambda item: item.get("at", ""))
        return {"entity": entity, "mentions": mentions, "relations": relations,
                "corrections": corrections, "timeline": timeline}


def json_safe_ids(correction):
    import json
    try:
        return json.dumps(correction.get("payload", {}), ensure_ascii=False)
    except TypeError:
        return ""


def ref_matches_correction(correction, entity):
    if not entity:
        return False
    blob = json_safe_ids(correction)
    eid = entity["entity_id"]
    if eid in blob:
        return True
    for name in [entity.get("canonical_name"), *entity.get("aliases", [])]:
        if name and f'"{name}"' in blob:
            return True
    return False
