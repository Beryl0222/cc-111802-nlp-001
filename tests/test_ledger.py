import json
import tempfile
import unittest
from pathlib import Path

from nlp_lesson_entity_ledger.repository import RecordRepository
from nlp_lesson_entity_ledger.text import extract_mentions
from nlp_lesson_entity_ledger.workflow import TextWorkflow


def make_record(record_id, revision, text, mentions, observed_at="2026-11-18T09:00:00Z"):
    return {
        "record_id": record_id,
        "source": "school-synthetic",
        "revision": revision,
        "observed_at": observed_at,
        "text": text,
        "mentions": list(mentions),
    }


LESSON_A_V1 = make_record(
    "lesson-a", 1, "三年级电路课:学生电源(学电)配小灯泡,学电输出限制6V。", ["学生电源", "学电", "小灯泡"]
)
LESSON_A_V2 = make_record(
    "lesson-a", 2, "三年级电路课:学生电源配小灯泡和开关。", ["学生电源", "小灯泡", "开关"], "2026-11-19T09:00:00Z"
)
LESSON_B_V1 = make_record(
    "lesson-b", 1, "四年级电学课:稳压电源与滑动变阻器配套使用。", ["稳压电源", "滑动变阻器"], "2026-11-18T09:05:00Z"
)
LESSON_B_V2 = make_record(
    "lesson-b",
    2,
    "四年级电学课:稳压电源(电源)与滑动变阻器配套,电源需教师预调。",
    ["稳压电源", "电源", "滑动变阻器"],
    "2026-11-19T09:05:00Z",
)


class LedgerTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state_path = Path(self.directory.name) / "state.json"
        self.workflow = TextWorkflow(RecordRepository(self.state_path))

    def mentions_by_id(self):
        return {mention["mention_id"]: mention for mention in self.workflow.list_mentions()}

    def relations_by_pair(self):
        return {(relation["subject"], relation["object"]): relation for relation in self.workflow.list_relations()}


class MentionExtractionTests(LedgerTestCase):
    def test_occurrence_inside_longer_surface_is_not_a_mention(self):
        record = {"text": "稳压电源(电源)与滑动变阻器配套,电源需教师预调。", "mentions": ["稳压电源", "电源", "滑动变阻器"]}
        mentions = extract_mentions(record)
        power = [mention for mention in mentions if mention["surface"] == "电源"]
        self.assertEqual([mention["occurrence"] for mention in power], [0, 1])

    def test_quoted_names_are_extracted_when_no_mentions_given(self):
        record = {"text": "使用「学生电源」和《电路基础》。", "mentions": []}
        surfaces = {mention["surface"] for mention in extract_mentions(record)}
        self.assertEqual(surfaces, {"学生电源", "电路基础"})

    def test_missing_surface_is_kept_with_empty_fragment(self):
        record = {"text": "只有这句话。", "mentions": ["不存在的器材"]}
        (mention,) = extract_mentions(record)
        self.assertEqual((mention["fragment"], mention["occurrence"]), ("", 0))


class IdentityTests(LedgerTestCase):
    def test_alias_merge_keeps_identity_across_revisions(self):
        self.workflow.ingest([LESSON_A_V1])
        self.workflow.ingest([LESSON_A_V2])
        self.assertEqual(len(self.workflow.list_entities()), 4)
        result = self.workflow.apply_corrections(
            [
                {
                    "type": "merge_entities",
                    "source": "auto:学电",
                    "target": "auto:学生电源",
                    "created_at": "2026-11-19T10:00:00Z",
                }
            ]
        )
        entities = {entity["entity_id"]: entity for entity in self.workflow.list_entities()}
        self.assertEqual(len(entities), 3)
        merged = entities["auto:学生电源"]
        self.assertEqual(merged["status"], "confirmed")
        self.assertEqual(merged["merged_ids"], ["auto:学电"])
        self.assertIn("学电", merged["aliases"])
        mentions = self.mentions_by_id()
        self.assertEqual(mentions["lesson-a@r1:学电#0"]["entity"], "auto:学生电源")
        self.assertEqual(mentions["lesson-a@r1:学电#1"]["entity"], "auto:学生电源")
        relation = self.relations_by_pair()[("auto:学生电源", "auto:小灯泡")]
        self.assertEqual([evidence["revision"] for evidence in relation["evidence"]], [1, 2])
        self.assertTrue(relation["current"])
        self.assertEqual(self.workflow.list_corrections()[0]["id"], result["correction_ids"][0])

    def test_same_name_entities_are_not_merged(self):
        self.workflow.ingest([LESSON_A_V1, LESSON_B_V1])
        self.workflow.ingest([LESSON_B_V2])
        self.workflow.apply_corrections(
            [
                {"type": "add_alias", "entity_id": "auto:学生电源", "alias": "电源"},
                {"type": "add_alias", "entity_id": "auto:稳压电源", "alias": "电源"},
            ]
        )
        mention = self.mentions_by_id()["lesson-b@r2:电源#0"]
        self.assertEqual(mention["status"], "uncertain")
        self.assertIsNone(mention["entity"])
        self.assertEqual(mention["candidates"], ["auto:学生电源", "auto:稳压电源"])
        entity_ids = {entity["entity_id"] for entity in self.workflow.list_entities()}
        self.assertIn("auto:学生电源", entity_ids)
        self.assertIn("auto:稳压电源", entity_ids)
        uncertain_pairs = {
            (relation["subject"], relation["object"]) for relation in self.workflow.list_relations(status="uncertain")
        }
        self.assertIn(("auto:学生电源", "auto:滑动变阻器"), uncertain_pairs)
        self.assertNotIn(("auto:稳压电源", "auto:滑动变阻器"), uncertain_pairs)

    def test_correction_may_arrive_after_automatic_processing(self):
        self.workflow.ingest([LESSON_B_V1, LESSON_B_V2])
        self.assertEqual(self.mentions_by_id()["lesson-b@r2:电源#0"]["entity"], "auto:电源")
        self.workflow.apply_corrections(
            [
                {"type": "add_alias", "entity_id": "auto:稳压电源", "alias": "电源"},
                {"type": "assign_mention", "mention_id": "lesson-b@r2:电源#0", "entity_id": "auto:稳压电源"},
            ]
        )
        mentions = self.mentions_by_id()
        self.assertEqual(mentions["lesson-b@r2:电源#0"]["status"], "confirmed")
        self.assertEqual(mentions["lesson-b@r2:电源#0"]["entity"], "auto:稳压电源")
        self.assertEqual(mentions["lesson-b@r2:电源#1"]["entity"], "auto:稳压电源")
        self.assertNotIn("auto:电源", {entity["entity_id"] for entity in self.workflow.list_entities()})

    def test_reprocessing_never_overrides_human_decisions(self):
        self.workflow.ingest([LESSON_A_V1, LESSON_B_V1])
        self.workflow.ingest([LESSON_A_V2, LESSON_B_V2])
        self.workflow.apply_corrections(
            [
                {"type": "merge_entities", "source": "auto:学电", "target": "auto:学生电源"},
                {"type": "add_alias", "entity_id": "auto:稳压电源", "alias": "电源"},
                {"type": "assign_mention", "mention_id": "lesson-b@r2:电源#0", "entity_id": "auto:稳压电源"},
            ]
        )
        entity_count = len(self.workflow.list_entities())
        self.workflow.ingest([LESSON_A_V1, LESSON_B_V1])
        self.workflow.ingest([LESSON_A_V2, LESSON_B_V2])
        mentions = self.mentions_by_id()
        self.assertEqual(mentions["lesson-a@r1:学电#0"]["entity"], "auto:学生电源")
        self.assertEqual(mentions["lesson-b@r2:电源#0"]["status"], "confirmed")
        self.assertEqual(mentions["lesson-b@r2:电源#0"]["entity"], "auto:稳压电源")
        self.assertEqual(len(self.workflow.list_entities()), entity_count)
        self.assertEqual(len(self.workflow.list_corrections()), 3)
        relation = self.relations_by_pair()[("auto:学生电源", "auto:小灯泡")]
        self.assertEqual(len(relation["evidence"]), 2)

    def test_older_revision_arriving_late_keeps_human_state(self):
        self.workflow.ingest([LESSON_A_V2])
        self.workflow.apply_corrections(
            [{"type": "assign_mention", "mention_id": "lesson-a@r2:开关#0", "entity_id": "auto:开关"}]
        )
        self.workflow.ingest([LESSON_A_V1])
        mentions = self.mentions_by_id()
        self.assertEqual(mentions["lesson-a@r2:开关#0"]["status"], "confirmed")
        self.assertIn("lesson-a@r1:学电#0", mentions)
        record = self.workflow.list_records()[0]
        self.assertEqual(record["revision"], 2)

    def test_reject_entity_preserves_uncertainty(self):
        self.workflow.ingest([LESSON_A_V1])
        self.workflow.apply_corrections(
            [{"type": "reject_entity", "mention_id": "lesson-a@r1:小灯泡#0", "entity_id": "auto:小灯泡"}]
        )
        mention = self.mentions_by_id()["lesson-a@r1:小灯泡#0"]
        self.assertEqual(mention["status"], "uncertain")
        self.assertIsNone(mention["entity"])
        self.assertEqual(mention["candidates"], [])
        pairs = set(self.relations_by_pair())
        self.assertNotIn(("auto:学生电源", "auto:小灯泡"), pairs)
        self.assertNotIn(("auto:学电", "auto:小灯泡"), pairs)
        self.assertIn(("auto:学生电源", "auto:学电"), pairs)

    def test_rename_suggestion_stays_uncertain_until_human_decides(self):
        self.workflow.ingest([LESSON_A_V1])
        self.workflow.ingest([LESSON_A_V2])
        suggestions = self.workflow.ledger()["suggestions"]
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0]["entity"], "auto:开关")
        self.assertEqual(suggestions[0]["candidates"], ["auto:学电"])
        self.assertEqual(suggestions[0]["confidence"], "single")
        self.assertEqual((suggestions[0]["from_revision"], suggestions[0]["to_revision"]), (1, 2))
        self.workflow.apply_corrections(
            [{"type": "confirm_distinct", "entity_a": "auto:开关", "entity_b": "auto:学电"}]
        )
        self.assertEqual(self.workflow.ledger()["suggestions"], [])

    def test_relation_provenance_traces_version_fragment_and_seq(self):
        first = make_record(
            "lesson-c", 1, "第一段:酒精灯加热烧杯。第二段:烧杯放在铁架台上。", ["酒精灯", "烧杯", "铁架台"]
        )
        second = make_record("lesson-c", 2, "第一段:酒精灯加热烧杯。", ["酒精灯", "烧杯"], "2026-11-19T09:00:00Z")
        self.workflow.ingest([first])
        self.workflow.ingest([second])
        relations = self.relations_by_pair()
        deleted = relations[("auto:烧杯", "auto:铁架台")]
        self.assertFalse(deleted["current"])
        (evidence,) = deleted["evidence"]
        self.assertEqual((evidence["record_id"], evidence["revision"]), ("lesson-c", 1))
        self.assertEqual(evidence["latest_revision"], 2)
        self.assertEqual(evidence["fragment"], "第二段:烧杯放在铁架台上。")
        self.assertTrue(evidence["seq"])
        self.assertEqual(evidence["mention_ids"], ["lesson-c@r1:烧杯#1", "lesson-c@r1:铁架台#0"])
        kept = relations[("auto:烧杯", "auto:酒精灯")]
        self.assertTrue(kept["current"])
        self.assertEqual([item["revision"] for item in kept["evidence"]], [1, 2])
        self.assertNotIn(("auto:酒精灯", "auto:铁架台"), relations)

    def test_create_entity_takes_over_its_surface(self):
        self.workflow.ingest([LESSON_A_V2])
        self.workflow.apply_corrections(
            [
                {"type": "create_entity", "name": "开关", "kind": "器材"},
                {"type": "assign_mention", "mention_id": "lesson-a@r2:开关#0", "entity_id": "E1"},
            ]
        )
        entities = {entity["entity_id"]: entity for entity in self.workflow.list_entities()}
        self.assertNotIn("auto:开关", entities)
        self.assertEqual(entities["E1"]["kind"], "器材")
        self.assertEqual(entities["E1"]["status"], "confirmed")
        mention = self.mentions_by_id()["lesson-a@r2:开关#0"]
        self.assertEqual((mention["status"], mention["entity"]), ("confirmed", "E1"))
        self.assertIn(("E1", "auto:学生电源"), set(self.relations_by_pair()))

    def test_corrections_are_atomic(self):
        self.workflow.ingest([LESSON_A_V1])
        with self.assertRaises(ValueError):
            self.workflow.apply_corrections(
                [
                    {"type": "merge_entities", "source": "auto:学电", "target": "auto:学生电源"},
                    {"type": "assign_mention", "mention_id": "missing", "entity_id": "auto:学电"},
                ]
            )
        self.assertEqual(self.workflow.list_corrections(), [])
        self.assertEqual(self.mentions_by_id()["lesson-a@r1:学电#0"]["entity"], "auto:学电")

    def test_invalid_corrections_are_rejected(self):
        self.workflow.ingest([LESSON_A_V1])
        with self.assertRaises(ValueError):
            self.workflow.apply_corrections([{"type": "unknown_type"}])
        self.workflow.apply_corrections(
            [{"type": "merge_entities", "source": "auto:学电", "target": "auto:学生电源"}]
        )
        with self.assertRaises(ValueError):
            self.workflow.apply_corrections(
                [{"type": "merge_entities", "source": "auto:学电", "target": "auto:学生电源"}]
            )
        with self.assertRaises(ValueError):
            self.workflow.apply_corrections(
                [{"type": "confirm_distinct", "entity_a": "auto:学电", "entity_b": "auto:学生电源"}]
            )

    def test_state_persists_across_instances(self):
        self.workflow.ingest([LESSON_A_V1])
        self.workflow.apply_corrections(
            [{"type": "merge_entities", "source": "auto:学电", "target": "auto:学生电源"}]
        )
        other = TextWorkflow(RecordRepository(self.state_path))
        self.assertEqual(len(other.list_corrections()), 1)
        self.assertEqual(
            {mention["mention_id"]: mention["entity"] for mention in other.list_mentions()},
            {mention["mention_id"]: mention["entity"] for mention in self.workflow.list_mentions()},
        )


class FixtureStoryTests(LedgerTestCase):
    def test_fixture_demo_flow(self):
        root = Path(__file__).resolve().parent.parent
        records = json.loads((root / "fixtures" / "records.json").read_text(encoding="utf-8"))
        revisions = json.loads((root / "fixtures" / "records-revision.json").read_text(encoding="utf-8"))
        corrections = json.loads((root / "fixtures" / "corrections.json").read_text(encoding="utf-8"))
        self.workflow.ingest(records)
        self.workflow.ingest(revisions)
        self.assertEqual(self.mentions_by_id()["lesson-b@r2:电源#0"]["entity"], "auto:电源")
        result = self.workflow.apply_corrections(corrections)
        self.assertEqual(result["applied"], len(corrections))
        self.assertTrue(all(item["applied"] for item in self.workflow.list_corrections()))
        mentions = self.mentions_by_id()
        self.assertEqual(mentions["lesson-b@r2:电源#0"]["entity"], "auto:稳压电源")
        self.assertEqual(mentions["lesson-b@r2:电源#0"]["status"], "confirmed")
        self.assertEqual(mentions["lesson-b@r2:电源#1"]["entity"], "auto:稳压电源")
        self.assertEqual(mentions["lesson-a@r1:学电#0"]["entity"], "auto:学生电源")
        entities = {entity["entity_id"]: entity for entity in self.workflow.list_entities()}
        self.assertNotIn("auto:电源", entities)
        self.assertEqual(entities["auto:学生电源"]["aliases"], ["学电", "电源"])
        relations = self.relations_by_pair()
        self.assertTrue(relations[("auto:学生电源", "auto:小灯泡")]["current"])
        self.assertTrue(relations[("auto:滑动变阻器", "auto:稳压电源")]["current"])
        summary = self.workflow.ledger_summary()
        self.assertEqual(summary["uncertain_mentions"], 0)
        self.assertEqual(summary["corrections"], len(corrections))


if __name__ == "__main__":
    unittest.main()
