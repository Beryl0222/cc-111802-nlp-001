from dataclasses import dataclass, asdict
from datetime import datetime


@dataclass(frozen=True)
class TextRecord:
    record_id: str
    source: str
    revision: int
    observed_at: str
    text: str
    context: str = ""
    mentions: tuple = ()

    @classmethod
    def from_dict(cls, value):
        mentions = []
        for item in value.get("mentions") or []:
            surface = item.get("surface") if isinstance(item, dict) else item
            surface = str(surface or "").strip()
            if not surface:
                raise ValueError("提及缺少名称")
            mentions.append({"surface": surface})
        record = cls(
            record_id=str(value.get("record_id", "")).strip(),
            source=str(value.get("source", "")).strip(),
            revision=int(value.get("revision", 0)),
            observed_at=str(value.get("observed_at", "")).strip(),
            text=str(value.get("text", "")),
            context=str(value.get("context", "")).strip(),
            mentions=tuple(mentions),
        )
        if not record.record_id or not record.source or record.revision < 1 or not record.text.strip():
            raise ValueError("文本记录缺少身份、来源、有效修订号或正文")
        datetime.fromisoformat(record.observed_at.replace("Z", "+00:00"))
        return record

    def to_dict(self):
        return asdict(self)
