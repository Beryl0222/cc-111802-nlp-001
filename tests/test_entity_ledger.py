import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from nlp_lesson_entity_ledger.cli import main as cli_main
from nlp_lesson_entity_ledger.ledger import EntityLedger
from nlp_lesson_entity_ledger.repository import RecordRepository
from nlp_lesson_entity_ledger.workflow import TextWorkflow


def make_workflow(directory, name="ledger.json"):
    return TextWorkflow(RecordRepository(Path(directory) / name))


def record(revision, text, *, record_id="lesson-1", observed_at=None, mentions=None, relations=None, source="syn"):
    return {
        "record_id": record_id,
        "source": source,
        "revision": revision,
        "observed_at": observed_at or f"2026-11-{18 + revision:02d}T09:00:00Z",
        "text": text,
        "mentions": mentions or [],
        "relations": relations or [],
    }


class IdentityContinuityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workflow = make_workflow(self.tmp.name)

    def test_exact_same_name_keeps_one_identity_across_versions(self):
        self.workflow.ingest([
            record(1, "电流表测量电流。", mentions=[{"id": "a1", "text": "电流表", "kind": "器材"}]),
            record(2, "学生观察电流表读数。", mentions=[{"id": "a2", "text": "电流表", "kind": "器材"}]),
        ])
        entities = self.workflow.list_entities()
        self.assertEqual(len(entities), 1)
        entity = entities[0]
        mentions = self.workflow.list_mentions()
        self.assertEqual(len(mentions), 2)
        # 第二次出现通过精确名称可靠链接到同一身份
        second = [m for m in mentions if m["client_id"] == "a2"][0]
        self.assertEqual(second["entity_id"], entity["entity_id"])
        self.assertEqual(second["resolution"], "exact")
        self.assertIn(second["mention_id"], entity["mention_ids"])

    def test_rename_stays_uncertain_until_human_merge(self):
        self.workflow.ingest([
            record(1, "滑动变阻器接入电路。",
                   mentions=[{"id": "r1", "text": "滑动变阻器", "kind": "器材"}]),
            record(2, "调节变阻器滑片。",
                   mentions=[{"id": "r2", "text": "变阻器", "kind": "器材"}]),
        ])
        new_mention = [m for m in self.workflow.list_mentions() if m["client_id"] == "r2"][0]
        self.assertEqual(new_mention["status"], "uncertain")
        self.assertIsNone(new_mention["entity_id"])
        # 简称相似只能产生候选，不能自动合并
        self.assertTrue(new_mention["candidate_ids"])
        self.assertEqual(len(self.workflow.list_entities()), 2)

        merged = self.workflow.merge_entities(source="滑动变阻器", target="变阻器")
        self.assertTrue(merged["entity_id"])
        self.assertEqual(len(self.workflow.list_entities()), 1)
        for mention in self.workflow.list_mentions():
            self.assertEqual(mention["entity_id"], merged["entity_id"])
            self.assertEqual(mention["resolution"], "manual")

    def test_human_alias_links_old_and_new_names(self):
        self.workflow.ingest([
            record(1, "安培表测电流。", mentions=["安培表"]),
            record(2, "电流表读数。", mentions=["电流表"]),
        ])
        entity = self.workflow.add_alias(alias="安培表", entity="电流表")
        self.assertEqual(entity["canonical_name"], "电流表")
        self.assertIn("安培表", entity["aliases"])
        rel = self.workflow.add_relation(source="甲组", relation="使用", target="安培表",
                                         record_id="lesson-1", revision=1, snippet="甲组使用安培表")
        self.assertEqual(rel["status"], "confirmed")
        found = self.workflow.query_relations(entity="电流表")
        self.assertEqual(len(found), 1)


class CorrectionPrecedenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "ledger.json"
        self.workflow = TextWorkflow(RecordRepository(self.path))
        self.v1 = record(1, "安培表测电流。", mentions=[{"id": "m2", "text": "安培表", "kind": "器材"}])
        self.workflow.ingest([self.v1])

    def test_late_correction_survives_reprocess_reingest_and_reload(self):
        mention = [m for m in self.workflow.list_mentions() if m["client_id"] == "m2"][0]
        target = self.workflow.list_entities()[0]
        linked = self.workflow.link_mention(mention="m2", entity=target["entity_id"],
                                            author="beryl", at="2026-11-21T10:00:00Z")
        self.assertEqual(linked["status"], "resolved")
        correction_id = linked["correction_id"]
        self.assertTrue(correction_id)

        # 重新处理旧版本
        self.workflow.reprocess("lesson-1", 1)
        again = [m for m in self.workflow.list_mentions() if m["client_id"] == "m2"][0]
        self.assertEqual(again["entity_id"], target["entity_id"])
        self.assertEqual(again["resolution"], "manual")
        self.assertEqual(again["correction_id"], correction_id)

        # 旧版本被重新导入（甚至和更新批次乱序）
        self.workflow.ingest([self.v1])
        again = [m for m in self.workflow.list_mentions() if m["client_id"] == "m2"][0]
        self.assertEqual(again["entity_id"], target["entity_id"])

        # 新实例从磁盘恢复后人工决定仍在
        reloaded = TextWorkflow(RecordRepository(self.path))
        again = [m for m in reloaded.list_mentions() if m["client_id"] == "m2"][0]
        self.assertEqual(again["entity_id"], target["entity_id"])
        self.assertEqual(len(reloaded.list_corrections()), 1)
        self.assertEqual(reloaded.list_corrections()[0]["author"], "beryl")

    def test_newer_correction_overrides_older_correction(self):
        e1 = self.workflow.list_entities()[0]
        other = self.workflow.add_entity("另一块表", "器材")
        self.workflow.link_mention(mention="m2", entity=e1["entity_id"])
        self.workflow.link_mention(mention="m2", entity=other["entity_id"])
        mention = [m for m in self.workflow.list_mentions() if m["client_id"] == "m2"][0]
        self.assertEqual(mention["entity_id"], other["entity_id"])
        actions = [c["action"] for c in self.workflow.list_corrections()]
        self.assertEqual(actions, ["register_entity", "link", "link"])
        # 最后一次链接决定生效
        self.assertEqual(self.workflow.list_corrections()[-1]["seq"], 3)

    def test_manual_entity_registration_persists_through_rebuild_and_reload(self):
        created = self.workflow.add_entity("定制器材", "器材", entity_id="kit-7")
        self.assertEqual(created["entity_id"], "kit-7")
        self.workflow.reprocess()
        self.assertIsNotNone(self.workflow.get_entity("kit-7"))
        reloaded = TextWorkflow(RecordRepository(self.path))
        self.assertIsNotNone(reloaded.get_entity("定制器材"))

    def test_invalid_correction_action_raises(self):
        with self.assertRaises(ValueError):
            self.workflow.add_correction({"action": "not-a-real-action"})


class HomographAndUncertainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workflow = make_workflow(self.tmp.name)

    def test_same_name_different_kind_or_setup_is_not_merged(self):
        self.workflow.ingest([
            record(1, "百叶箱和烧杯里各有一支温度计。", record_id="L2",
                   mentions=[
                       {"id": "t1", "text": "温度计", "kind": "气象仪器", "discriminator": "百叶箱"},
                       {"id": "t2", "text": "温度计", "kind": "实验器材", "discriminator": "烧杯"},
                   ])
        ])
        mentions = {m["client_id"]: m for m in self.workflow.list_mentions()}
        self.assertEqual(mentions["t1"]["status"], "uncertain")
        self.assertEqual(mentions["t2"]["status"], "uncertain")
        self.assertTrue(mentions["t2"]["ambiguous"])
        self.assertTrue(mentions["t2"]["candidate_ids"])
        # 同名的两个不同器材保留为两个身份
        thermometers = [e for e in self.workflow.list_entities() if e["canonical_name"] == "温度计"]
        self.assertEqual(len(thermometers), 2)

    def test_review_queue_and_status_filter(self):
        self.workflow.ingest([record(1, "陌生仪器。", mentions=["陌生仪器"])])
        self.assertEqual(len(self.workflow.uncertain_mentions()), 1)
        self.assertEqual(len(self.workflow.list_mentions(status="uncertain")), 1)
        self.assertEqual(len(self.workflow.list_entities(status="pending")), 1)
        queue = self.workflow.review_queue()
        self.assertIn("uncertain_mentions", queue)

    def test_confirm_entity_resolves_proposal_mentions(self):
        self.workflow.ingest([record(1, "电流表。", mentions=["电流表"])])
        entity = self.workflow.list_entities()[0]
        self.assertEqual(entity["status"], "uncertain")
        confirmed = self.workflow.confirm_entity(entity["entity_id"])
        self.assertEqual(confirmed["status"], "confirmed")
        mention = self.workflow.list_mentions()[0]
        self.assertEqual(mention["status"], "resolved")
        self.assertEqual(mention["entity_id"], entity["entity_id"])


class RelationProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workflow = make_workflow(self.tmp.name)
        self.v1 = record(
            1, "甲组把安培表接入电路，电源为灯泡供电。",
            mentions=[
                {"id": "g1", "text": "甲组", "kind": "学生组"},
                {"id": "a1", "text": "安培表", "kind": "器材"},
                {"id": "p1", "text": "电源", "kind": "器材"},
                {"id": "b1", "text": "灯泡", "kind": "器材"},
            ],
            relations=[
                {"subject": "甲组", "relation": "使用", "object": "安培表"},
                {"subject": "电源", "relation": "供电给", "object": "灯泡"},
            ],
        )
        self.v2 = record(
            2, "第一小组把电流表接好；电池组让小灯泡亮起来。",
            mentions=[
                {"id": "g2", "text": "第一小组", "kind": "学生组"},
                {"id": "a2", "text": "电流表", "kind": "器材"},
                {"id": "p2", "text": "电池组", "kind": "器材"},
                {"id": "b2", "text": "小灯泡", "kind": "器材"},
            ],
            relations=[
                {"subject": "第一小组", "relation": "使用", "object": "电流表"},
                {"subject": "电池组", "relation": "供电给", "object": "小灯泡"},
            ],
        )
        self.workflow.ingest([self.v1, self.v2])
        for source, target in [("甲组", "第一小组"), ("安培表", "电流表"),
                               ("电源", "电池组"), ("灯泡", "小灯泡")]:
            self.workflow.merge_entities(source=source, target=target)

    def test_renames_do_not_duplicate_knowledge(self):
        relations = self.workflow.query_relations()
        labels = {(r["subject_name"], r["relation"], r["object_name"]) for r in relations}
        self.assertEqual(labels, {
            ("第一小组", "使用", "电流表"),
            ("电池组", "供电给", "小灯泡"),
        })
        using = self.workflow.query_relations(entity="电流表", relation_type="使用")
        self.assertEqual(len(using), 1)
        self.assertEqual(len(using[0]["evidence"]), 2)
        self.assertEqual(sorted(e["revision"] for e in using[0]["evidence"]), [1, 2])

    def test_provenance_has_version_snippet_and_revision(self):
        relation = self.workflow.query_relations(entity="电流表", relation_type="使用")[0]
        evidence = relation["evidence"]
        for item in evidence:
            self.assertTrue(item["record_id"])
            self.assertIn(item["revision"], (1, 2))
            self.assertTrue(item["snippet"])
            self.assertIn("version", item)
        old = [e for e in evidence if e["revision"] == 1][0]
        self.assertIn("安培表", old["snippet"])
        provenance = self.workflow.relation_provenance(relation["relation_id"])
        self.assertEqual(len(provenance), 2)

    def test_deleted_paragraph_becomes_historical_but_traceable(self):
        # 新版本删去了使用安培表的段落，也不再提供关系
        v3 = record(3, "本课改为观看实验视频。", mentions=[], relations=[])
        self.workflow.ingest([v3])
        relations = self.workflow.query_relations(entity="电流表", relation_type="使用")
        self.assertEqual(len(relations), 1)
        rel = relations[0]
        self.assertFalse(rel["current"])
        self.assertTrue(rel["historical"])
        # 仍然能查清来自哪个旧版本、哪次修订与原文片段
        old = [e for e in rel["evidence"] if e["revision"] == 1][0]
        self.assertEqual(old["record_id"], "lesson-1")
        self.assertTrue(old["snippet"])
        # current_only 不返回历史关系
        self.assertEqual(self.workflow.query_relations(current_only=True), [])

    def test_source_target_pair_filter(self):
        result = self.workflow.query_relations(source="电池组", target="小灯泡")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["relation"], "供电给")
        self.assertEqual(self.workflow.query_relations(source="不存在", target="小灯泡"), [])

    def test_merge_of_relation_only_names_unifies_relations(self):
        workflow = make_workflow(self.tmp.name, "relation-only.json")
        workflow.ingest([
            record(1, "安培表。", record_id="ro",
                   mentions=[{"mention_id": "m1", "text": "安培表"}],
                   relations=[{"subject": "我组", "predicate": "使用", "object": "安培表"}]),
            record(2, "电流表。", record_id="ro",
                   mentions=[{"mention_id": "m2", "text": "电流表"}],
                   relations=[{"subject": "我们组", "predicate": "使用", "object": "电流表"}]),
        ])
        workflow.merge_entities("安培表", "电流表")
        workflow.merge_entities("我组", "我们组")
        rels = workflow.query_relations(record_id="ro", relation_type="使用")
        self.assertEqual(len(rels), 1)
        self.assertEqual(len(rels[0]["evidence"]), 2)

    def test_manual_relation_carries_correction_provenance(self):
        relation = self.workflow.add_relation(
            source="第一小组", relation="记录", target="电流表",
            snippet="第一小组记录电流表读数", author="reviewer-li")
        self.assertEqual(relation["status"], "confirmed")
        evidence = relation["evidence"][-1]
        self.assertEqual(evidence["origin"], "manual")
        self.assertTrue(evidence["correction_id"])
        self.assertEqual(evidence["snippet"], "第一小组记录电流表读数")

    def test_retract_relation_hides_by_default_and_shows_with_flag(self):
        before = len(self.workflow.query_relations(relation_type="供电给"))
        self.assertEqual(before, 1)
        self.workflow.add_correction("remove_relation", {
            "source": "电池组", "relation": "供电给", "target": "小灯泡"})
        self.assertEqual(self.workflow.query_relations(relation_type="供电给"), [])
        retracted = self.workflow.query_relations(relation_type="供电给", include_retracted=True)
        self.assertEqual(len(retracted), 1)
        self.assertEqual(retracted[0]["status"], "retracted")

    def test_trace_entity_returns_mentions_corrections_relations(self):
        trace = self.workflow.trace_entity("电流表")
        self.assertGreaterEqual(len(trace["mentions"]), 2)
        self.assertTrue(trace["relations"])
        self.assertTrue(trace["corrections"])
        kinds = {item["kind"] for item in trace["timeline"]}
        self.assertEqual(kinds, {"mention", "correction"})


class FlexibleInputAndViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workflow = make_workflow(self.tmp.name)

    def test_string_mentions_triple_relations_and_nested_analysis(self):
        value = {
            "record_id": "flex", "source": "s", "revision": 1,
            "observed_at": "2026-11-18T09:00:00Z",
            "text": "甲使用电流表。",
            "mentions": ["电流表"],
            "relations": [["甲", "使用", "电流表"]],
            "analysis": {"mentions": [{"text": "甲", "kind": "学生"}]},
        }
        self.workflow.ingest([value])
        texts = {m["text"] for m in self.workflow.list_mentions()}
        self.assertEqual(texts, {"电流表", "甲"})
        relations = self.workflow.query_relations()
        self.assertEqual(len(relations), 1)
        self.assertEqual(relations[0]["relation"], "使用")

    def test_merge_into_brand_new_target_name(self):
        self.workflow.ingest([record(1, "安培表。", mentions=["安培表"])])
        merged = self.workflow.merge_entities("安培表", "电流表")
        self.assertEqual(merged["canonical_name"], "电流表")
        # 旧名称仍可查询并重定向到新身份
        self.assertEqual(self.workflow.get_entity("安培表")["entity_id"], merged["entity_id"])
        self.assertEqual(len(self.workflow.list_entities()), 1)

    def test_concept_graph_nodes_edges(self):
        self.workflow.ingest([
            record(1, "甲用电流表。",
                   mentions=["甲", "电流表"],
                   relations=[{"subject": "甲", "relation": "使用", "object": "电流表"}])
        ])
        graph = self.workflow.concept_graph()
        self.assertIn("nodes", graph)
        self.assertIn("edges", graph)
        self.assertEqual(len(graph["nodes"]), 2)
        self.assertEqual(len(graph["edges"]), 1)
        edge = graph["edges"][0]
        self.assertEqual(edge["label"], "使用")
        self.assertTrue(edge["source"] and edge["target"])
        # 近义图结构键
        self.assertIn("vertices", graph)
        self.assertIn("links", graph)

    def test_callable_collection_views(self):
        self.workflow.ingest([record(1, "电流表。", mentions=["电流表"])])
        self.assertIsInstance(self.workflow.entities, list)
        self.assertEqual(len(self.workflow.entities), 1)
        self.assertEqual(len(self.workflow.entities(status="uncertain")), 1)
        self.assertEqual(len(self.workflow.mentions()), 1)
        self.assertEqual(len(self.workflow.corrections), 0)
        self.assertEqual(len(self.workflow.relations()), 0)

    def test_gazetteer_extracts_without_explicit_mentions(self):
        workflow = TextWorkflow(
            RecordRepository(Path(self.tmp.name) / "g.json"),
            gazetteer=["电流表", "电池组"],
        )
        workflow.ingest([record(1, "电流表与电池组。")])
        texts = {m["text"] for m in workflow.list_mentions()}
        self.assertEqual(texts, {"电流表", "电池组"})

    def test_adhoc_add_mention(self):
        mention = self.workflow.add_mention({"record_id": "X", "revision": 2, "text": "新仪器"})
        self.assertEqual(mention["text"], "新仪器")
        self.assertEqual(len(self.workflow.list_mentions(record_id="X")), 1)

    def test_old_repository_format_is_upgraded(self):
        path = Path(self.tmp.name) / "old.json"
        path.write_text(json.dumps({"records": []}), encoding="utf-8")
        workflow = TextWorkflow(RecordRepository(path))
        self.assertEqual(workflow.ingest([record(1, "文本")]), {"received": 1, "stored": 1})
        state = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("entities", state)
        self.assertIn("corrections", state)


class StandaloneLedgerTests(unittest.TestCase):
    def test_in_memory_ledger_and_alias_class(self):
        ledger = EntityLedger()
        result = ledger.ingest([record(1, "电流表。", mentions=["电流表"])])
        self.assertEqual(result, {"received": 1, "stored": 1})
        self.assertEqual(len(ledger.list_entities()), 1)
        ledger.add_correction(action="confirm", entity=ledger.list_entities()[0]["entity_id"])
        self.assertEqual(ledger.list_entities()[0]["status"], "confirmed")


class CliTests(unittest.TestCase):
    def test_cli_ingest_correct_query_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            state = str(Path(directory) / "state.json")
            source = Path(directory) / "in.json"
            source.write_text(json.dumps([
                record(1, "甲用电流表。", mentions=[{"id": "a", "text": "电流表"}],
                       relations=[{"subject": "甲", "relation": "用", "object": "电流表"}])
            ], ensure_ascii=False), encoding="utf-8")

            out = io.StringIO()
            with redirect_stdout(out):
                code = cli_main(["ingest", str(source), "--state", state])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out.getvalue())["stored"], 1)

            out = io.StringIO()
            with redirect_stdout(out):
                cli_main(["correct", "--state", state, "--action", "confirm", "--entity", "电流表"])
            correction = json.loads(out.getvalue())
            self.assertEqual(correction["action"], "confirm")

            out = io.StringIO()
            with redirect_stdout(out):
                cli_main(["relations", "--state", state, "--entity", "电流表"])
            relations = json.loads(out.getvalue())
            self.assertEqual(len(relations), 1)

            relation_id = relations[0]["relation_id"]
            out = io.StringIO()
            with redirect_stdout(out):
                cli_main(["provenance", "--state", state, relation_id])
            provenance = json.loads(out.getvalue())
            self.assertEqual(provenance[0]["record_id"], "lesson-1")


if __name__ == "__main__":
    unittest.main()
