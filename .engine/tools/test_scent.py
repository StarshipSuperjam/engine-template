"""test_scent.py — the per-prompt attention scent handler: scent.py (memory-substrate-sqlite-fts5, slice 5, PR 2).

Run via the engine's CI command:
    uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py'

The scent is the M1-completing seam: a UserPromptSubmit hook that injects ATTRIBUTED POINTERS over memory's
fast lookup. These pin its locked laws: silent on no strong match; attributed pointers NOT content (the injected
text never quotes a record's body and always carries the verify clause); dedup (a pointer surfaced once a session
is not re-injected, keyed on record id, a new topic still surfaces); the FTS5-absent one-time slower-mode
disclosure (never a per-prompt slow scan); does-not-reinforce; and fail-open (no prompt / no memory module ->
silent). Isolation is a throwaway ENGINE_MEMORY_DIR cabinet (the handler's default-path lookup lands there) plus
explicit cleanup of the OS-temp surfaced-set the dedup uses.
"""

import inspect
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scent  # noqa: E402
from memory import index, ledger, records  # noqa: E402

_ID = records.RECORD_ID_KEY


def _inject(decision):
    """The text a handler decision injects, or None when it stays silent (proceed)."""
    if isinstance(decision, dict) and decision.get("action") == "inject":
        return decision.get("context", "")
    return None


class _ScentBase(unittest.TestCase):
    """A throwaway ENGINE_MEMORY_DIR cabinet; the handler's default-path scent_lookup lands there. Session ids
    are unique per test (derived from the temp dir) and their OS-temp surfaced-sets are cleared on teardown."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="engine-scent-test-")
        self._prev = os.environ.get(ledger.ENV_DIR)
        os.environ[ledger.ENV_DIR] = self.tmp
        self.now = int(time.time())
        self._sids = set()

    def tearDown(self):
        for sid in self._sids:
            scent._clear(sid)
        if self._prev is None:
            os.environ.pop(ledger.ENV_DIR, None)
        else:
            os.environ[ledger.ENV_DIR] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sid(self, suffix=""):
        s = f"test-{os.path.basename(self.tmp)}-{suffix}"
        self._sids.add(s)
        return s

    def add(self, text, *, role="observation", tags=(), with_id=True):
        record = {"ts": self.now, "role": role, "tags": list(tags), "text": text}
        if with_id:
            record[_ID] = records.new_record_id()
        ledger.append(record, path=ledger.ledger_path())
        return record.get(_ID)

    def rebuild(self):
        index.rebuild()

    def run_scent(self, prompt, session_id):
        return _inject(scent.handler({"prompt": prompt, "session_id": session_id}))

    def seed_calendar(self):
        """A small corpus with a DISTINCTIVE 'calendar' term + ordinary-word distractors."""
        rid = self.add("we decided to build the calendar sync against the user's calendar",
                       role="decision", tags=["scheduling", "calendar-sync"])
        self.add("keep the onboarding copy short", role="lesson", tags=["onboarding"])
        self.add("prefer snake_case for config keys", role="preference", tags=["naming"])
        self.rebuild()
        return rid


class FiringTests(_ScentBase):
    def test_strong_match_injects_a_pointer(self):
        self.seed_calendar()
        text = self.run_scent("how should we handle the calendar sync?", self.sid())
        self.assertIsNotNone(text)

    def test_near_miss_is_silent(self):
        self.seed_calendar()
        self.assertIsNone(self.run_scent("what is the weather like today?", self.sid()))

    def test_common_words_only_is_silent(self):
        self.seed_calendar()
        self.assertIsNone(self.run_scent("the and now to of", self.sid()))

    def test_empty_memory_is_silent_then_lights_up(self):
        # The inert -> live crossover: silent with nothing stored, a pointer once memory is filled + indexed.
        sid = self.sid()
        self.assertIsNone(self.run_scent("calendar sync", sid))
        self.seed_calendar()
        scent._clear(sid)
        self.assertIsNotNone(self.run_scent("calendar sync", sid))

    def test_no_prompt_is_silent(self):
        self.seed_calendar()
        self.assertIsNone(_inject(scent.handler({"session_id": self.sid()})))
        self.assertIsNone(_inject(scent.handler({"prompt": "   ", "session_id": self.sid()})))

    def test_missing_memory_module_is_silent(self):
        # The inert-seam state: no memory package -> the lazy import fails -> silent, never a fault. Force the
        # `from memory import ...` to raise (patching sys.modules won't: the submodule is already bound).
        import builtins
        self.seed_calendar()
        real_import = builtins.__import__

        def fail_memory(name, *a, **k):
            if name == "memory" or name.startswith("memory."):
                raise ImportError("no memory module")
            return real_import(name, *a, **k)

        with mock.patch("builtins.__import__", side_effect=fail_memory):
            self.assertIsNone(self.run_scent("calendar sync", self.sid()))


class RenderLawTests(_ScentBase):
    def test_injected_text_excludes_the_record_body(self):
        body = "we decided to build the calendar sync against the user's calendar"
        self.seed_calendar()
        text = self.run_scent("calendar sync", self.sid()) or ""
        self.assertTrue(text)
        self.assertNotIn(body, text)               # pointers, NOT content
        self.assertNotIn("snake_case", text)       # no other body leaks either

    def test_injected_text_carries_the_verify_clause(self):
        self.seed_calendar()
        text = self.run_scent("calendar sync", self.sid()) or ""
        self.assertIn("verify before asserting", text)

    def test_names_role_and_tags(self):
        self.seed_calendar()
        text = self.run_scent("calendar sync", self.sid()) or ""
        self.assertIn("decision", text)
        self.assertIn("calendar-sync", text)       # a tag is a pointer (entity ref), surfaced; the body is not

    def test_caps_at_surface_max(self):
        # The cap is a rendering bound, independent of bm25 magnitude (which other tests cover). Lower the
        # salience bar to 0 so every match qualifies, isolating that _undeduped caps the output at _SURFACE_MAX.
        for i in range(scent._SURFACE_MAX + 4):
            self.add(f"calendar planning note {i}", role="decision", tags=[f"e{i}"])
        self.rebuild()
        with mock.patch.object(scent, "_threshold", return_value=0.0):
            text = self.run_scent("calendar", self.sid()) or ""
        self.assertGreaterEqual(text.count("- a recorded"), 1)                  # non-vacuous: it DID surface
        self.assertEqual(text.count("- a recorded"), scent._SURFACE_MAX)        # capped at exactly the max


class DedupTests(_ScentBase):
    def test_surfaced_once_per_session(self):
        self.seed_calendar()
        sid = self.sid()
        self.assertIsNotNone(self.run_scent("calendar sync", sid))
        self.assertIsNone(self.run_scent("calendar sync", sid))   # same id already surfaced -> silent

    def test_a_new_topic_still_surfaces(self):
        cal = self.add("the calendar sync decision", role="decision", tags=["scheduling"])
        exp = self.add("the export pipeline rewrite", role="decision", tags=["export"])
        for t in ("onboarding copy stays short", "prefer snake_case names", "the nightly cache rebuild",
                  "dark mode everywhere", "retries capped at three"):
            self.add(t)   # distractors so 'calendar' and 'export' each keep a high bm25 IDF (fire reliably)
        self.rebuild()
        sid = self.sid()
        first = self.run_scent("calendar", sid)
        second = self.run_scent("export pipeline", sid)            # a DIFFERENT record -> still surfaces
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertNotEqual(cal, exp)

    def test_garbled_session_id_does_not_crash(self):
        self.seed_calendar()
        # No usable session id -> no dedup state, but still answers (and never raises).
        self.assertIsNotNone(_inject(scent.handler({"prompt": "calendar sync", "session_id": ""})))
        self.assertIsNotNone(_inject(scent.handler({"prompt": "calendar sync", "session_id": None})))


class DegradedTests(_ScentBase):
    def test_fts5_absent_discloses_once_then_silent(self):
        self.seed_calendar()
        sid = self.sid()
        original = index.fts5_available
        index.fts5_available = lambda *a, **k: False
        try:
            first = self.run_scent("calendar sync", sid)
            second = self.run_scent("calendar sync", sid)
        finally:
            index.fts5_available = original
        self.assertIsNotNone(first)
        self.assertIn("paused", first)             # the one-time slower-mode disclosure
        self.assertIsNone(second)                  # not repeated

    def test_missing_index_is_silent_not_degraded(self):
        self.add("calendar sync decided", role="decision")   # ledger written, index NOT rebuilt
        self.assertIsNone(self.run_scent("calendar sync", self.sid()))


class NoSideEffectTests(_ScentBase):
    def test_scent_adds_no_reinforcement(self):
        self.seed_calendar()
        def marks():
            return sum(1 for r in ledger.iter_records(path=ledger.ledger_path())
                       if isinstance(r, dict) and r.get("kind") == records.REINFORCEMENT_KIND)
        before = marks()
        self.run_scent("calendar sync", self.sid())
        self.assertEqual(marks() - before, 0)      # the push is not usage

    def test_source_has_no_reinforce_or_write_calls(self):
        src = "".join(inspect.getsource(fn) for fn in
                      (scent.handler, scent._render, scent._pointer_line, scent._undeduped, scent._score_of))
        self.assertNotIn("record_access", src)
        self.assertNotIn("ledger.append", src)


class FailOpenTests(_ScentBase):
    def test_a_crash_in_the_lookup_fails_open(self):
        # The handler rides run_hook's fail-open; a fault injects nothing and never stalls the turn.
        import io, json as _json
        self.seed_calendar()
        with mock.patch.object(index, "scent_lookup", side_effect=RuntimeError("boom")):
            out, err = io.StringIO(), io.StringIO()
            code = scent.hooks.run_hook(
                "UserPromptSubmit", scent.handler,
                stdin=io.StringIO(_json.dumps({"prompt": "calendar sync", "session_id": self.sid()})),
                stdout=out, stderr=err)
        self.assertNotEqual(code, 2)               # never a hard block
        self.assertEqual(out.getvalue().strip(), "")  # injected nothing


if __name__ == "__main__":
    unittest.main()
