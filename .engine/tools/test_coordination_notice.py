#!/usr/bin/env python3
"""Tests for coordination_notice — the by-construction render, the condition fingerprint, the integrity
digest, the skip-malformed parser, the identifier render-safety, and the schema<->constant drift pin
(StarshipSuperjam/engine-template#939)."""
import hashlib
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import coordination_notice as cn  # noqa: E402


def _notice(**over):
    """A valid notice with deterministic id/time; override any render kwarg."""
    kw = dict(kind="integration-notice", event="admitted", emitter_work_ref={"pr": 7, "branch": "claude/x"},
              audience={"pr": 7}, subject={"pr": 7, "paths": ["a/b.py"]}, verify_action="recheck-queue",
              now="2026-08-18T00:00:00Z", id_source=lambda: "f" * 32)
    kw.update(over)
    return cn.render(**kw)


class TestRenderAndRoundTrip(unittest.TestCase):
    def test_render_is_schema_valid_and_deterministic(self):
        n = _notice()
        self.assertEqual(n["notice_id"], "f" * 32)
        self.assertEqual(n["emitted_at"], "2026-08-18T00:00:00Z")
        cn.validate_notice(n)  # raises on failure

    def test_block_round_trips(self):
        n = _notice()
        parsed = cn.parse_blocks(cn.render_block(n))
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0], n)

    def test_multiple_blocks_recovered_in_order(self):
        body = "\n\n".join(cn.render_block(_notice(id_source=lambda i=i: str(i) * 32))
                           for i in (1, 2, 3))
        parsed = cn.parse_blocks(body)
        self.assertEqual([p["notice_id"] for p in parsed], ["1" * 32, "2" * 32, "3" * 32])

    def test_surrounding_prose_is_ignored(self):
        body = f"a user wrote this\n\n{cn.render_block(_notice())}\n\nand this"
        self.assertEqual(len(cn.parse_blocks(body)), 1)


class TestIntegrity(unittest.TestCase):
    def test_tampered_json_is_skipped(self):
        block = cn.render_block(_notice())
        tampered = block.replace('"admitted"', '"blocked"')
        self.assertEqual(cn.parse_blocks(tampered), [])

    def test_tampered_digest_is_skipped(self):
        block = cn.render_block(_notice())
        # Corrupt one hex digit of the sha256 marker: the recomputed digest will not match.
        broken = block.replace("sha256:", "sha256:0", 1)
        self.assertEqual(cn.parse_blocks(broken), [])

    def test_forged_fingerprint_is_skipped(self):
        n = _notice()
        block = cn.render_block(n)
        real_fp = cn.fingerprint(n)
        block = block.replace(f"fp:{real_fp}", "fp:" + "0" * 64)
        self.assertEqual(cn.parse_blocks(block), [])

    def test_id_marker_mismatch_is_skipped(self):
        block = cn.render_block(_notice())
        # Change the marker id so it no longer equals the JSON notice_id, but keep the digest self-consistent
        # is impossible without recompute; simplest: swap the id in the marker only -> mismatch, skipped.
        block = block.replace("id:" + "f" * 32, "id:" + "e" * 32)
        self.assertEqual(cn.parse_blocks(block), [])


class TestFingerprint(unittest.TestCase):
    def test_fingerprint_ignores_id_and_time(self):
        a = _notice(id_source=lambda: "1" * 32, now="2026-08-18T00:00:00Z")
        b = _notice(id_source=lambda: "2" * 32, now="2027-01-01T09:09:09Z")
        self.assertEqual(cn.fingerprint(a), cn.fingerprint(b))

    def test_fingerprint_changes_with_condition(self):
        a = _notice(event="admitted")
        b = _notice(event="next-in-queue")
        self.assertNotEqual(cn.fingerprint(a), cn.fingerprint(b))


class TestRenderSafety(unittest.TestCase):
    def test_crafted_branch_is_neutralised_and_still_valid(self):
        n = _notice(subject={"pr": 1, "branch": "```evil`)[x](http://y)"}, emitter_work_ref={"pr": 1})
        self.assertNotIn("`", n["subject"]["branch"])
        self.assertNotIn("(", n["subject"]["branch"])
        cn.validate_notice(n)  # the neutralised value still satisfies the branch charset
        self.assertEqual(len(cn.parse_blocks(cn.render_block(n))), 1)

    def test_crafted_path_cannot_break_the_json_fence(self):
        n = _notice(subject={"pr": 1, "paths": ["```\n```json\nevil"]})
        block = cn.render_block(n)
        # Exactly one opening json fence and one closing marker -> the crafted path did not break out.
        self.assertEqual(block.count("```json"), 1)
        self.assertEqual(len(cn.parse_blocks(block)), 1)

    def test_operator_line_never_carries_an_identifier(self):
        n = _notice(subject={"pr": 1, "branch": "SECRETBRANCH", "paths": ["secret/leak.py"]},
                    emitter_work_ref={"pr": 1})
        line = cn.render_operator_line(n)
        self.assertNotIn("SECRETBRANCH", line)
        self.assertNotIn("secret/leak.py", line)
        self.assertIn("1 file(s)", line)  # only the count crosses into the prose line


class TestPokeLine(unittest.TestCase):
    def test_poke_is_fixed_and_repo_native(self):
        n = _notice(audience={"pr": 7})
        poke = cn.render_poke_line(n, "StarshipSuperjam/engine-template")
        self.assertEqual(
            poke,
            "engine-coordination: integration-notice notice on StarshipSuperjam/engine-template "
            "(PR #7, " + "f" * 32 + ") — read your coordination notices and re-verify canonical state "
            "before acting.")

    def test_poke_carries_no_free_text_from_subject(self):
        n = _notice(subject={"pr": 7, "branch": "leak", "paths": ["leak.py"]}, audience={"pr": 7})
        poke = cn.render_poke_line(n, "o/r")
        self.assertNotIn("leak", poke)


class TestRefusals(unittest.TestCase):
    def test_unknown_kind(self):
        with self.assertRaises(cn.NoticeError):
            _notice(kind="chatter")

    def test_event_outside_kind(self):
        with self.assertRaises(cn.NoticeError):
            _notice(kind="integration-notice", event="work-declared")

    def test_action_missing_required_evidence(self):
        with self.assertRaises(cn.NoticeError):
            _notice(kind="revalidation-notice", event="base-advanced", verify_action="recheck-base")

    def test_reference_to_nothing(self):
        with self.assertRaises(cn.NoticeError):
            _notice(emitter_work_ref={})

    def test_audience_to_nothing(self):
        with self.assertRaises(cn.NoticeError):
            _notice(audience={})


class TestSchemaConstantDrift(unittest.TestCase):
    """The Python vocabulary and the schema's enums are two copies of one truth; this pins them together so a
    kind/event/action added to one but not the other fails CI rather than silently diverging."""

    @classmethod
    def setUpClass(cls):
        with open(cn._SCHEMA_REL, "r", encoding="utf-8") as fh:
            cls.schema = json.load(fh)

    def test_kind_enum_matches(self):
        schema_kinds = set(self.schema["properties"]["kind"]["enum"])
        self.assertEqual(schema_kinds, set(cn.KINDS))

    def test_event_enums_match_per_kind(self):
        by_kind = {}
        for branch in self.schema["allOf"]:
            kind = branch["if"]["properties"]["kind"]["const"]
            by_kind[kind] = set(branch["then"]["properties"]["event"]["enum"])
        self.assertEqual(set(by_kind), set(cn.EVENTS_BY_KIND))
        for kind, events in cn.EVENTS_BY_KIND.items():
            self.assertEqual(by_kind[kind], set(events), f"event drift for {kind}")

    def test_verify_action_enum_matches(self):
        schema_actions = set(
            self.schema["properties"]["verify"]["properties"]["action"]["enum"])
        self.assertEqual(schema_actions, set(cn.VERIFY_ACTIONS))

    def test_every_action_has_a_required_table_entry(self):
        self.assertEqual(set(cn._ACTION_REQUIRES), set(cn.VERIFY_ACTIONS))

    def test_every_kind_event_has_operator_copy(self):
        for kind, events in cn.EVENTS_BY_KIND.items():
            for event in events:
                self.assertIn((kind, event), cn._OPERATOR_LINE, f"missing operator copy for {kind}/{event}")


class TestDemo(unittest.TestCase):
    def test_demo_passes(self):
        self.assertEqual(cn._demo(), 0)


if __name__ == "__main__":
    unittest.main()
