import json
from pathlib import Path


STATE_FORMAT = "lesson-entity-ledger/1"


def default_state():
    return {
        "format": STATE_FORMAT,
        "records": [],
        "versions": [],
        "entities": [],
        "mentions": [],
        "corrections": [],
        "relations": [],
        "relation_intents": [],
        "counters": {"correction": 0},
    }


class RecordRepository:
    def __init__(self, path):
        candidate = Path(path)
        # 允许直接传入状态目录；文件约定为其中的 records.json。
        if candidate.exists() and candidate.is_dir():
            candidate = candidate / "records.json"
        self.path = candidate

    # ---- 向后兼容的记录接口 ----
    def read(self):
        return self.read_state()["records"]

    def replace(self, records):
        # 严格保持旧契约：只写 {"records": [...]}。
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps({"records": records}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)

    # ---- 完整台账状态 ----
    def read_state(self):
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return default_state()
        value = json.loads(raw)
        if not isinstance(value, dict) or not isinstance(value.get("records"), list):
            raise ValueError("记录库格式无效")
        state = default_state()
        state.update(value)
        # 补齐新版本字段，兼容只含 records 的旧文件。
        for key, default_value in default_state().items():
            if key not in state:
                if isinstance(default_value, (list, dict)):
                    state[key] = json.loads(json.dumps(default_value))
                else:
                    state[key] = default_value
        if not isinstance(state.get("counters"), dict):
            state["counters"] = {"correction": 0}
        return state

    def write_state(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)
        return self.path

    def replace_state(self, state):
        return self.write_state(state)
