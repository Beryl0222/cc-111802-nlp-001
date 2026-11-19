"""实体身份台账。

版本摄取(自动处理)与人工纠正都记录为带序号的事件,实体、提及和关系
等派生状态由事件日志重建,因此:

- 人工纠正可以随时到达,始终优先于同目标的自动结果;
- 重新处理旧版本只替换该版本的摄取事件,不会覆盖较新的人工决定;
- 无法可靠延续身份时保留不确定状态(提及候选、实体改名建议、候选关系);
- 每条关系都记录来源版本、原文片段与处理批次,供教研员复核。
"""

from datetime import datetime, timezone
from itertools import combinations

CORRECTION_TYPES = (
    "create_entity",
    "add_alias",
    "merge_entities",
    "confirm_distinct",
    "assign_mention",
    "reject_entity",
)


def empty_state():
    return {"seq": 0, "entity_counter": 0, "events": []}


def mention_id(record_id, revision, surface, occurrence):
    return f"{record_id}@r{revision}:{surface}#{occurrence}"


def append_ingest(state, record, mentions):
    """记录一次版本摄取。同一 (record_id, revision) 重复摄取只保留最新事件。"""
    key = (record["record_id"], record["revision"])
    state["events"] = [
        event
        for event in state["events"]
        if not (event["type"] == "ingest" and (event["record_id"], event["revision"]) == key)
    ]
    state["seq"] += 1
    state["events"].append(
        {
            "type": "ingest",
            "seq": state["seq"],
            "record_id": record["record_id"],
            "revision": record["revision"],
            "observed_at": record.get("observed_at", ""),
            "mentions": [dict(mention) for mention in mentions],
        }
    )
    return state["seq"]


def append_correction(state, correction):
    """追加一条人工纠正。调用方须先用 validate_correction 校验。"""
    state["seq"] += 1
    event = {
        "type": "correction",
        "seq": state["seq"],
        "id": f"C{state['seq']}",
        "correction_type": correction["type"],
        "created_at": correction.get("created_at") or datetime.now(timezone.utc).isoformat(),
        "note": str(correction.get("note", "")),
        "payload": {
            key: value
            for key, value in correction.items()
            if key not in ("type", "created_at", "note")
        },
    }
    if correction["type"] == "create_entity":
        state["entity_counter"] += 1
        event["entity_id"] = f"E{state['entity_counter']}"
    state["events"].append(event)
    return event


def _canon(merged_into, entity_id):
    seen = set()
    while entity_id in merged_into and entity_id not in seen:
        seen.add(entity_id)
        entity_id = merged_into[entity_id]
    return entity_id


def rebuild(state):
    """由事件日志重建派生状态。相同的日志总是重建出相同的结果。"""
    events = sorted(state.get("events", []), key=lambda event: event["seq"])
    ingests = {}
    corrections = []
    for event in events:
        if event["type"] == "ingest":
            ingests[(event["record_id"], event["revision"])] = event
        elif event["type"] == "correction":
            corrections.append(event)
    ingest_list = sorted(ingests.values(), key=lambda event: (event["record_id"], event["revision"]))

    # 第一遍:人工纠正决定的实体结构(人工实体、别名、合并、提及覆盖)。
    human = {}
    extra_aliases = {}
    merged_into = {}
    rejected_pairs = set()
    overrides = {}
    touched = {}
    for event in corrections:
        kind = event["correction_type"]
        payload = event["payload"]
        cid = event["id"]
        if kind == "create_entity":
            human[event["entity_id"]] = {
                "canonical_name": payload["name"],
                "kind": payload.get("kind", "unknown"),
            }
            touched[event["entity_id"]] = cid
        elif kind == "add_alias":
            alias = payload["alias"]
            aliases = extra_aliases.setdefault(payload["entity_id"], [])
            if alias not in aliases:
                aliases.append(alias)
            touched[_canon(merged_into, payload["entity_id"])] = cid
        elif kind == "merge_entities":
            source = _canon(merged_into, payload["source"])
            target = _canon(merged_into, payload["target"])
            if source != target:
                merged_into[source] = target
                rejected_pairs.discard(frozenset((source, target)))
                touched[target] = cid
        elif kind == "confirm_distinct":
            rejected_pairs.add(
                frozenset(
                    (
                        _canon(merged_into, payload["entity_a"]),
                        _canon(merged_into, payload["entity_b"]),
                    )
                )
            )
        elif kind == "assign_mention":
            overrides[payload["mention_id"]] = {"kind": "assign", "entity": payload["entity_id"], "by": cid}
            touched[_canon(merged_into, payload["entity_id"])] = cid
        elif kind == "reject_entity":
            overrides[payload["mention_id"]] = {"kind": "reject", "entity": payload["entity_id"], "by": cid}

    # 第二遍:未被任何实体名称认领的表面形式获得自动实体,身份随事件日志稳定。
    claimed = set()
    for entity in human.values():
        claimed.add(entity["canonical_name"])
    for aliases in extra_aliases.values():
        claimed.update(aliases)
    surfaces = []
    for event in ingest_list:
        for mention in event["mentions"]:
            if mention["surface"] not in surfaces:
                surfaces.append(mention["surface"])
    entities = {}
    for entity_id, entity in human.items():
        entities[entity_id] = {
            "entity_id": entity_id,
            "canonical_name": entity["canonical_name"],
            "aliases": list(extra_aliases.get(entity_id, [])),
            "kind": entity["kind"],
            "origin": "human",
        }
    for surface in surfaces:
        if surface not in claimed:
            entity_id = f"auto:{surface}"
            entities[entity_id] = {
                "entity_id": entity_id,
                "canonical_name": surface,
                "aliases": list(extra_aliases.get(entity_id, [])),
                "kind": "unknown",
                "origin": "auto",
            }

    # 名称索引:所有实体(含被合并者)的名称都指向合并后的实体。
    index = {}
    for entity_id, entity in entities.items():
        canon_id = _canon(merged_into, entity_id)
        for name in {entity["canonical_name"], *entity["aliases"]}:
            index.setdefault(name, set()).add(canon_id)

    # 第三遍:解析提及。人工覆盖优先;同名多实体保留不确定。
    mentions = {}
    record_versions = {}
    for event in ingest_list:
        record_id, revision = event["record_id"], event["revision"]
        record_versions.setdefault(record_id, set()).add(revision)
        for mention in event["mentions"]:
            mid = mention_id(record_id, revision, mention["surface"], mention["occurrence"])
            targets = sorted(index.get(mention["surface"], ()))
            override = overrides.get(mid)
            if override and override["kind"] == "assign":
                entity = _canon(merged_into, override["entity"])
                status, decided_by, candidates = "confirmed", override["by"], [entity]
            elif (
                override
                and override["kind"] == "reject"
                and _canon(merged_into, override["entity"]) in targets
            ):
                rejected = _canon(merged_into, override["entity"])
                entity, status, decided_by = None, "uncertain", override["by"]
                candidates = [target for target in targets if target != rejected]
            elif len(targets) == 1:
                entity, status, decided_by, candidates = targets[0], "auto", "auto", targets
            else:
                entity, status, decided_by, candidates = None, "uncertain", "auto", targets
            mentions[mid] = {
                "mention_id": mid,
                "record_id": record_id,
                "revision": revision,
                "surface": mention["surface"],
                "occurrence": mention["occurrence"],
                "fragment": mention["fragment"],
                "entity": entity,
                "status": status,
                "candidates": candidates,
                "decided_by": decided_by,
                "ingest_seq": event["seq"],
                "observed_at": event.get("observed_at", ""),
            }

    # 改名建议:同一记录相邻修订间消失的旧实体与新出现的自动实体,
    # 只保留为不确定建议,由人工确认合并或确认不同。
    suggestions = {}
    for record_id, revisions in record_versions.items():
        ordered = sorted(revisions)
        for previous, current in zip(ordered, ordered[1:]):
            previous_entities = {
                mention["entity"]
                for mention in mentions.values()
                if mention["record_id"] == record_id and mention["revision"] == previous and mention["entity"]
            }
            current_entities = {
                mention["entity"]
                for mention in mentions.values()
                if mention["record_id"] == record_id and mention["revision"] == current and mention["entity"]
            }
            gone = previous_entities - current_entities
            new = {
                entity_id
                for entity_id in current_entities - previous_entities
                if entities.get(entity_id, {}).get("origin") == "auto"
            }
            for entity_id in sorted(new):
                candidates = sorted(
                    gone_entity
                    for gone_entity in gone
                    if gone_entity != entity_id
                    and frozenset((gone_entity, entity_id)) not in rejected_pairs
                )
                if candidates:
                    suggestions.setdefault(entity_id, []).append(
                        {
                            "candidates": candidates,
                            "record_id": record_id,
                            "from_revision": previous,
                            "to_revision": current,
                            "confidence": "single" if len(new) == 1 and len(gone) == 1 else "multiple",
                        }
                    )

    # 关系:同句共现的已解析实体形成关系;不确定提及只贡献候选关系。
    relations = {}
    for event in ingest_list:
        record_id, revision = event["record_id"], event["revision"]
        latest_revision = max(record_versions[record_id])
        by_fragment = {}
        for mention in event["mentions"]:
            mid = mention_id(record_id, revision, mention["surface"], mention["occurrence"])
            by_fragment.setdefault(mention["fragment"], []).append(mid)
        for fragment, mention_ids in by_fragment.items():
            resolved = {}
            uncertain = []
            for mid in mention_ids:
                mention = mentions[mid]
                if mention["entity"]:
                    entry = resolved.setdefault(mention["entity"], {"mention_ids": [], "statuses": []})
                    entry["mention_ids"].append(mid)
                    entry["statuses"].append(mention["status"])
                elif mention["candidates"]:
                    uncertain.append((mention["candidates"], mid))
            pairs = {}
            for first, second in combinations(sorted(resolved), 2):
                pairs[(first, second)] = {
                    "mention_ids": list(resolved[first]["mention_ids"] + resolved[second]["mention_ids"]),
                    "statuses": list(resolved[first]["statuses"] + resolved[second]["statuses"]),
                    "uncertain": False,
                }
            for candidates, mid in uncertain:
                for candidate in candidates:
                    for other in sorted(resolved):
                        if other == candidate or (candidate in resolved and other in resolved):
                            continue
                        key = (min(candidate, other), max(candidate, other))
                        pair = pairs.setdefault(key, {"mention_ids": [], "statuses": [], "uncertain": True})
                        pair["mention_ids"].append(mid)
                        pair["uncertain"] = True
            for (first, second), pair in pairs.items():
                relation = relations.setdefault(
                    (first, second), {"subject": first, "object": second, "kind": "co_mention", "evidence": []}
                )
                if pair["uncertain"] or "uncertain" in pair["statuses"]:
                    status = "uncertain"
                elif pair["statuses"] and all(item == "confirmed" for item in pair["statuses"]):
                    status = "confirmed"
                else:
                    status = "auto"
                relation["evidence"].append(
                    {
                        "record_id": record_id,
                        "revision": revision,
                        "latest_revision": latest_revision,
                        "fragment": fragment,
                        "seq": event["seq"],
                        "observed_at": event.get("observed_at", ""),
                        "mention_ids": sorted(set(pair["mention_ids"])),
                        "status": status,
                    }
                )
    relation_list = []
    for relation in relations.values():
        statuses = [evidence["status"] for evidence in relation["evidence"]]
        relation["status"] = (
            "uncertain"
            if "uncertain" in statuses
            else ("confirmed" if all(item == "confirmed" for item in statuses) else "auto")
        )
        relation["current"] = any(
            evidence["revision"] == evidence["latest_revision"] for evidence in relation["evidence"]
        )
        relation["evidence"].sort(key=lambda evidence: (evidence["record_id"], evidence["revision"]))
        relation_list.append(relation)
    relation_list.sort(key=lambda relation: (relation["subject"], relation["object"]))

    # 实体视图:合并类合并展示,保留成员与人工确认状态。
    classes = {}
    for entity_id in entities:
        classes.setdefault(_canon(merged_into, entity_id), []).append(entity_id)
    entity_list = []
    for canon_id, members in classes.items():
        names = set()
        kinds = set()
        for member in members:
            names.add(entities[member]["canonical_name"])
            names.update(entities[member]["aliases"])
            kinds.add(entities[member]["kind"])
        canonical = entities[canon_id]["canonical_name"]
        origin = "human" if any(entities[member]["origin"] == "human" for member in members) else "auto"
        entity_list.append(
            {
                "entity_id": canon_id,
                "canonical_name": canonical,
                "aliases": sorted(names - {canonical}),
                "kind": sorted(kinds - {"unknown"})[0] if kinds - {"unknown"} else "unknown",
                "origin": origin,
                "status": "confirmed"
                if origin == "human" or any(member in touched for member in members)
                else "auto",
                "merged_ids": sorted(member for member in members if member != canon_id),
                "mention_count": sum(1 for mention in mentions.values() if mention["entity"] == canon_id),
                "possible_matches": suggestions.get(canon_id, []),
            }
        )
    entity_list.sort(key=lambda entity: entity["entity_id"])

    entity_index = {entity_id: _canon(merged_into, entity_id) for entity_id in entities}
    correction_list = []
    for event in corrections:
        payload = event["payload"]
        kind = event["correction_type"]
        applied, detail = True, ""
        if kind in ("assign_mention", "reject_entity"):
            applied = payload["mention_id"] in mentions
            if not applied:
                detail = "提及不存在,对应版本可能已被改写"
        elif kind == "add_alias":
            applied = payload["entity_id"] in entity_index
            if not applied:
                detail = "目标实体不存在"
        elif kind == "merge_entities":
            applied = payload["source"] in entity_index and payload["target"] in entity_index
            if not applied:
                detail = "源或目标实体不存在"
        elif kind == "confirm_distinct":
            applied = payload["entity_a"] in entity_index and payload["entity_b"] in entity_index
            if not applied:
                detail = "实体不存在"
        correction_list.append(
            {
                "id": event["id"],
                "seq": event["seq"],
                "type": kind,
                "created_at": event["created_at"],
                "note": event.get("note", ""),
                "payload": payload,
                "applied": applied,
                "detail": detail,
            }
        )

    return {
        "seq": state.get("seq", 0),
        "entities": entity_list,
        "entity_index": entity_index,
        "mentions": sorted(mentions.values(), key=lambda mention: mention["mention_id"]),
        "relations": relation_list,
        "suggestions": [
            {"entity": entity_id, **suggestion}
            for entity_id, items in suggestions.items()
            for suggestion in items
        ],
        "corrections": correction_list,
        "record_versions": {record_id: sorted(revisions) for record_id, revisions in record_versions.items()},
    }


def validate_correction(derived, correction):
    """校验一条人工纠正是否可应用于当前派生状态,不合法时抛出 ValueError。"""
    if not isinstance(correction, dict):
        raise ValueError("纠正必须是对象")
    kind = correction.get("type")
    if kind not in CORRECTION_TYPES:
        raise ValueError(f"未知纠正类型: {kind}")
    entity_index = derived["entity_index"]
    mention_ids = {mention["mention_id"] for mention in derived["mentions"]}

    def require_entity(key):
        entity_id = str(correction.get(key, "")).strip()
        if entity_id not in entity_index:
            raise ValueError(f"实体不存在: {entity_id or key}")
        return entity_id

    def require_mention():
        mid = str(correction.get("mention_id", ""))
        if mid not in mention_ids:
            raise ValueError(f"提及不存在: {mid or 'mention_id'}")
        return mid

    if kind == "create_entity":
        if not str(correction.get("name", "")).strip():
            raise ValueError("create_entity 需要非空 name")
    elif kind == "add_alias":
        require_entity("entity_id")
        if not str(correction.get("alias", "")).strip():
            raise ValueError("add_alias 需要非空 alias")
    elif kind == "merge_entities":
        source = require_entity("source")
        target = require_entity("target")
        if entity_index[source] == entity_index[target]:
            raise ValueError("两个实体已指向同一对象")
    elif kind == "confirm_distinct":
        first = require_entity("entity_a")
        second = require_entity("entity_b")
        if entity_index[first] == entity_index[second]:
            raise ValueError("实体已合并,无法标记为不同")
    elif kind in ("assign_mention", "reject_entity"):
        require_mention()
        require_entity("entity_id")
