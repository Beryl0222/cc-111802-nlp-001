import argparse
import json
from pathlib import Path

from .repository import RecordRepository
from .workflow import TextWorkflow


def main():
    parser = argparse.ArgumentParser(description="本地教研文本处理工具")
    parser.add_argument(
        "action",
        choices=("ingest", "list", "entities", "mentions", "relations", "corrections", "correct", "summary"),
    )
    parser.add_argument("path", nargs="?", default="fixtures/records.json")
    parser.add_argument("--state", default="var/records.json")
    parser.add_argument("--source")
    parser.add_argument("--record")
    parser.add_argument("--status")
    parser.add_argument("--entity")
    parser.add_argument("--current-only", action="store_true")
    args = parser.parse_args()
    workflow = TextWorkflow(RecordRepository(args.state))
    if args.action == "ingest":
        result = workflow.ingest(json.loads(Path(args.path).read_text(encoding="utf-8")))
    elif args.action == "correct":
        result = workflow.apply_corrections(json.loads(Path(args.path).read_text(encoding="utf-8")))
    elif args.action == "list":
        result = workflow.list_records(args.source)
    elif args.action == "entities":
        result = workflow.list_entities()
    elif args.action == "mentions":
        result = workflow.list_mentions(record_id=args.record, status=args.status)
    elif args.action == "relations":
        result = workflow.list_relations(
            entity_id=args.entity, status=args.status, current_only=args.current_only
        )
    elif args.action == "corrections":
        result = workflow.list_corrections()
    else:
        result = workflow.ledger_summary()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
