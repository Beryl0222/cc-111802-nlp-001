import argparse
import json
from pathlib import Path

from .repository import RecordRepository
from .workflow import TextWorkflow


def _load_json_argument(raw):
    if raw is None:
        return None
    text = raw.strip()
    if text.startswith("{") or text.startswith("["):
        return json.loads(text)
    path = Path(text)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    # 否则视为内联 JSON 字段失败时的普通字符串
    return raw


def build_parser():
    parser = argparse.ArgumentParser(description="本地教研课案实体台账工具")
    parser.add_argument(
        "action",
        choices=(
            "ingest", "list",
            "entities", "mentions", "corrections", "correct",
            "relations", "query", "provenance", "trace", "reprocess",
        ),
    )
    parser.add_argument("path", nargs="?", default=None,
                        help="ingest/correct 的 JSON 文件；其他命令可作为 id/名称位置参数")
    parser.add_argument("--state", default="var/records.json")
    parser.add_argument("--source", dest="source_filter", default=None, help="记录来源过滤")
    parser.add_argument("--record", "--record-id", dest="record_id", default=None)
    parser.add_argument("--revision", type=int, default=None)
    parser.add_argument("--status", default=None)
    parser.add_argument("--kind", default=None)
    parser.add_argument("--query", default=None)
    parser.add_argument("--entity", default=None)
    parser.add_argument("--relation", "--relation-type", dest="relation_type", default=None)
    parser.add_argument("--source-endpoint", dest="source_endpoint", default=None)
    parser.add_argument("--target-endpoint", dest="target_endpoint", default=None)
    parser.add_argument("--current-only", action="store_true")
    parser.add_argument("--include-retracted", action="store_true")
    # correct 专用
    parser.add_argument("--action", "--action-name", dest="correction_action", default=None)
    parser.add_argument("--json", dest="json_blob", default=None, help="完整纠正 JSON（字符串或 @文件）")
    parser.add_argument("--author", default=None)
    parser.add_argument("--at", default=None)
    parser.add_argument("--mention", default=None)
    parser.add_argument("--target", default=None)
    parser.add_argument("--name", default=None)
    parser.add_argument("--alias", default=None)
    parser.add_argument("--new-name", dest="new_name", default=None)
    return parser


def _correction_from_args(args):
    if args.json_blob:
        blob = args.json_blob
        if blob.startswith("@"):
            return json.loads(Path(blob[1:]).read_text(encoding="utf-8"))
        return json.loads(blob)
    if args.path and Path(args.path).exists():
        loaded = json.loads(Path(args.path).read_text(encoding="utf-8"))
        return loaded

    data = {}
    if args.correction_action:
        data["action"] = args.correction_action
    if args.author:
        data["author"] = args.author
    if args.at:
        data["at"] = args.at
    if args.mention:
        data["mention_id"] = args.mention
    if args.entity:
        data["entity"] = args.entity
    if args.target:
        data["target"] = args.target
    if args.source_endpoint:
        data["source"] = args.source_endpoint
    if args.name:
        data["name"] = args.name
    if args.alias:
        data["alias"] = args.alias
    if args.new_name:
        data["new_name"] = args.new_name
    if args.relation_type:
        data["relation"] = args.relation_type
    if args.record_id:
        data["record_id"] = args.record_id
    if args.revision is not None:
        data["revision"] = args.revision
    return data


def main(argv=None):
    parser = build_parser()
    args, extra = parser.parse_known_args(argv)
    # argparse 对 “两个位置参数 + 可选参数夹在中间 + 末尾位置参数” 有解析缺陷，
    # 这里把漏到 extra 里的末尾位置参数补回 path。
    if extra:
        unknown_options = [item for item in extra if item.startswith("-")]
        trailing = [item for item in extra if not item.startswith("-")]
        if unknown_options:
            parser.error(f"unrecognized arguments: {' '.join(unknown_options)}")
        if args.path is None and trailing:
            args.path = trailing.pop(0)
        if trailing:
            parser.error(f"unrecognized arguments: {' '.join(trailing)}")
    workflow = TextWorkflow(RecordRepository(args.state))

    if args.action == "ingest":
        input_path = args.path or "fixtures/records.json"
        values = json.loads(Path(input_path).read_text(encoding="utf-8"))
        result = workflow.ingest(values)
    elif args.action == "list":
        result = workflow.list_records(args.source_filter)
    elif args.action == "entities":
        result = workflow.list_entities(status=args.status, kind=args.kind, query=args.query or args.path)
    elif args.action == "mentions":
        result = workflow.list_mentions(record_id=args.record_id, revision=args.revision, status=args.status)
    elif args.action == "corrections":
        result = workflow.list_corrections()
    elif args.action == "correct":
        correction = _correction_from_args(args)
        if isinstance(correction, list):
            result = workflow.ledger.ingest_corrections(correction)
        elif not correction.get("action"):
            parser.error("correct 需要 --action、--json 或纠正 JSON 文件")
        else:
            result = workflow.add_correction(correction)
    elif args.action in ("relations", "query"):
        result = workflow.query_relations(
            entity=args.entity or args.path,
            relation_type=args.relation_type,
            current_only=args.current_only,
            record_id=args.record_id,
            include_retracted=args.include_retracted,
            source=args.source_endpoint,
            target=args.target_endpoint,
        )
    elif args.action == "provenance":
        relation_id = args.entity or args.path
        result = workflow.relation_provenance(relation_id)
    elif args.action == "trace":
        ref = args.entity or args.path
        result = workflow.trace_entity(ref)
    elif args.action == "reprocess":
        record_id = args.record_id or args.path
        result = workflow.reprocess(record_id, args.revision)
    else:  # pragma: no cover - argparse choices 已限制
        parser.error(f"未知操作 {args.action}")
        return 2

    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
