import json
from pathlib import Path


class RecordRepository:
    def __init__(self, path):
        self.path = Path(path)

    def read_state(self):
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"records": [], "ledger": None}
        if not isinstance(value, dict) or not isinstance(value.get("records"), list):
            raise ValueError("记录库格式无效")
        ledger = value.get("ledger")
        if ledger is not None and not isinstance(ledger, dict):
            raise ValueError("记录库格式无效")
        return {"records": value["records"], "ledger": ledger}

    def read(self):
        return self.read_state()["records"]

    def read_ledger(self):
        return self.read_state()["ledger"]

    def replace(self, records):
        self._write(records, self.read_state()["ledger"])

    def write_ledger(self, ledger):
        self._write(self.read_state()["records"], ledger)

    def _write(self, records, ledger):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"records": records}
        if ledger is not None:
            payload["ledger"] = ledger
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)
