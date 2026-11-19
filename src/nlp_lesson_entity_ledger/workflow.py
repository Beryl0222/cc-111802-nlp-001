from .ledger import append_correction, append_ingest, empty_state, rebuild, validate_correction
from .records import TextRecord
from .text import annotate, extract_mentions, normalize_text


class TextWorkflow:
    def __init__(self, repository):
        self.repository = repository

    def ingest(self, values):
        if not isinstance(values, list):
            raise TypeError("导入内容必须是记录列表")
        incoming = [TextRecord.from_dict(value).to_dict() for value in values]
        existing = self.repository.read()
        current = {record["record_id"]: record for record in existing}
        ledger = self.repository.read_ledger() or empty_state()
        for record in incoming:
            record["text"] = normalize_text(record["text"])
            record["analysis"] = annotate(record)
            previous = current.get(record["record_id"])
            if previous is None or record["revision"] >= previous["revision"]:
                current[record["record_id"]] = record
            # 台账接受所有版本(包括重新处理的旧版本),人工纠正不受影响。
            append_ingest(ledger, record, extract_mentions(record))
        result = sorted(current.values(), key=lambda row: row["record_id"])
        self.repository.replace(result)
        self.repository.write_ledger(ledger)
        return {"received": len(incoming), "stored": len(result)}

    def apply_corrections(self, values):
        if not isinstance(values, list):
            raise TypeError("纠正内容必须是列表")
        ledger = self.repository.read_ledger() or empty_state()
        applied = []
        for value in values:
            validate_correction(rebuild(ledger), value)
            applied.append(append_correction(ledger, value))
        self.repository.write_ledger(ledger)
        return {"applied": len(applied), "correction_ids": [event["id"] for event in applied]}

    def ledger(self):
        return rebuild(self.repository.read_ledger() or empty_state())

    def list_records(self, source=None):
        records = self.repository.read()
        if source:
            records = [record for record in records if record["source"] == source]
        return records

    def list_entities(self):
        return self.ledger()["entities"]

    def list_mentions(self, record_id=None, status=None):
        mentions = self.ledger()["mentions"]
        if record_id:
            mentions = [mention for mention in mentions if mention["record_id"] == record_id]
        if status:
            mentions = [mention for mention in mentions if mention["status"] == status]
        return mentions

    def list_relations(self, entity_id=None, status=None, current_only=False):
        derived = self.ledger()
        relations = derived["relations"]
        if entity_id:
            canon_id = derived["entity_index"].get(entity_id)
            if canon_id is None:
                raise ValueError(f"未知实体: {entity_id}")
            relations = [
                relation
                for relation in relations
                if relation["subject"] == canon_id or relation["object"] == canon_id
            ]
        if status:
            relations = [relation for relation in relations if relation["status"] == status]
        if current_only:
            relations = [relation for relation in relations if relation["current"]]
        return relations

    def list_corrections(self):
        return self.ledger()["corrections"]

    def ledger_summary(self):
        derived = self.ledger()
        return {
            "entities": len(derived["entities"]),
            "mentions": len(derived["mentions"]),
            "uncertain_mentions": sum(1 for mention in derived["mentions"] if mention["status"] == "uncertain"),
            "relations": len(derived["relations"]),
            "uncertain_relations": sum(1 for relation in derived["relations"] if relation["status"] == "uncertain"),
            "suggestions": len(derived["suggestions"]),
            "corrections": len(derived["corrections"]),
            "seq": derived["seq"],
        }
