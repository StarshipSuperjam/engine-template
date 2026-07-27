"""Tests for local-reference containment — the declared vocabulary and the outbound scan.

Verifies: the declaration reader tells its three states apart (absent / declared / unreadable) and never
reports an unreadable file as an empty one; a declared string is escaped, so it can never act as a pattern;
each of the three declared shapes matches its own form and no other; `section_refs` catches a citation while
leaving alone the capability prose that names the same document (the discrimination the whole shape exists
for); the diff reader returns added lines with line numbers and reports an unreadable diff as UNINSPECTED
rather than empty; renames are not allowed to carry content past the scan; the declaration file does not
match itself; findings are soft and name only the matched token; and the demo runs.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import local_references as lr  # noqa: E402


def _decl(tmp, obj):
    p = os.path.join(tmp, "operator-local-references.json")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(obj if isinstance(obj, str) else __import__("json").dumps(obj))
    return p


class TestReaderStates(unittest.TestCase):
    def test_absent_is_the_silent_empty_state(self):
        with tempfile.TemporaryDirectory() as d:
            vocab, state = lr.load_vocabulary(os.path.join(d, "nope.json"))
        self.assertEqual((vocab, state), ([], lr.ABSENT))

    def test_unparseable_is_reported_distinctly_never_as_absent(self):
        # The distinction is the whole point: a caller that cannot tell these apart would narrate an unread
        # declaration as "checked and clean", which is a false claim of cleanliness.
        with tempfile.TemporaryDirectory() as d:
            vocab, state = lr.load_vocabulary(_decl(d, "{not json"))
        self.assertEqual((vocab, state), ([], lr.UNREADABLE))

    def test_a_non_object_declaration_is_unreadable_not_empty(self):
        with tempfile.TemporaryDirectory() as d:
            _vocab, state = lr.load_vocabulary(_decl(d, ["ACME-"]))
        self.assertEqual(state, lr.UNREADABLE)

    def test_a_real_declaration_compiles_and_reports_declared(self):
        with tempfile.TemporaryDirectory() as d:
            vocab, state = lr.load_vocabulary(_decl(d, {"id_prefixes": ["ACME-"]}))
        self.assertEqual(state, lr.DECLARED)
        self.assertEqual([(k, t) for k, t, _p in vocab], [("id_prefixes", "ACME-")])

    def test_degenerate_and_non_string_members_are_dropped(self):
        # Belt-and-braces behind the hard shape gate: a single character would match nearly every line.
        vocab = lr.compile_vocabulary({"id_prefixes": ["ACME-", "D", "", 7], "phrases": ["  "]})
        self.assertEqual([t for _k, t, _p in vocab], ["ACME-"])


class TestDeclaredStringsAreNeverPatterns(unittest.TestCase):
    def test_a_regex_metacharacter_is_matched_literally(self):
        # The declaration is operator text. If it were compiled unescaped, `.*` would match everything and a
        # malformed one would raise inside the reader.
        vocab = lr.compile_vocabulary({"phrases": [".*"]})
        self.assertEqual(lr.scan(vocab, lines=[("a.py", 1, "anything at all")]), [])
        hits = lr.scan(vocab, lines=[("a.py", 1, "the literal .* token")])
        self.assertEqual([h["token"] for h in hits], [".*"])

    def test_an_unbalanced_bracket_does_not_raise(self):
        vocab = lr.compile_vocabulary({"phrases": ["ACME-["]})
        self.assertEqual(lr.scan(vocab, lines=[("a.py", 1, "ACME-[ here")])[0]["token"], "ACME-[")


class TestShapes(unittest.TestCase):
    def setUp(self):
        self.vocab = lr.compile_vocabulary({
            "id_prefixes": ["ACME-"], "phrases": ["Acme Handbook"], "section_refs": ["acme-topology"]})

    def _tokens(self, text):
        return [h["token"] for h in lr.scan(self.vocab, lines=[("a.py", 1, text)])]

    def test_id_prefix_needs_digits_and_its_own_boundary(self):
        self.assertEqual(self._tokens("see ACME-156 for why"), ["ACME-156"])
        self.assertEqual(self._tokens("the AACME-156 part"), [])       # a letter on the left
        self.assertEqual(self._tokens("D- with no number"), [])     # no digits
        self.assertEqual(self._tokens("ACME-156-migration notes"), ["ACME-156"])  # hyphen-joined still counts

    def test_a_phrase_matches_only_on_its_own_boundaries(self):
        self.assertEqual(self._tokens("follow the Acme Handbook"), ["Acme Handbook"])
        self.assertEqual(self._tokens("the acme handbookish thing"), [])

    def test_section_ref_catches_a_citation_and_leaves_capability_prose_alone(self):
        # THE discrimination this shape exists for. The bare document name appears both in a citation (the
        # defect) and in prose naming the rule it stands for (the FIX). Matching the bare name would flag the
        # wording that resolves the defect — a check firing on its own remedy trains people to ignore it.
        self.assertEqual(self._tokens("kept out of git (acme-topology Law 5)"),
                         ["acme-topology Law 5"])
        self.assertEqual(self._tokens("stays a viewing surface — the acme-topology rule"), [])
        self.assertEqual(self._tokens("the acme-topology wall"), [])

    def test_section_markers_are_a_closed_set(self):
        for cited in ("acme-topology §4", "acme-topology Section 2", "acme-topology Law 5"):
            self.assertTrue(self._tokens(cited), cited)
        self.assertEqual(self._tokens("acme-topology paragraph 5"), [])


class TestScanSurfaces(unittest.TestCase):
    def setUp(self):
        self.vocab = lr.compile_vocabulary({"id_prefixes": ["ACME-"]})

    def test_a_path_name_is_scanned_as_well_as_a_line(self):
        hits = lr.scan(self.vocab, paths=["docs/ACME-156-migration.md", "src/ok.py"])
        self.assertEqual([(h["where"], h["token"]) for h in hits], [("docs/ACME-156-migration.md", "ACME-156")])

    def test_the_pull_request_prose_is_scanned(self):
        # The body travels to the other repository exactly as the diff does, and is where this project's own
        # convention parks decision references — so a clean diff with a citation in the body is not clean.
        hits = lr.scan(self.vocab, blobs={"the pull-request description": "line one\nper ACME-309, restore it"})
        self.assertEqual([(h["where"], h["line"], h["token"]) for h in hits],
                         [("the pull-request description", 2, "ACME-309")])

    def test_the_declaration_file_does_not_match_itself(self):
        # Its own added lines contain the declared strings by definition; reporting the operator's vocabulary
        # back to them as a leak on every edit to it would be pure noise.
        self.assertEqual(lr.scan(self.vocab, lines=[(lr.DECLARATION_REL, 2, '  "id_prefixes": ["ACME-156"]')]), [])


class TestDiffReader(unittest.TestCase):
    _DIFF = (b"diff --git a/src/app.py b/src/app.py\n"
             b"--- a/src/app.py\n+++ b/src/app.py\n"
             b"@@ -0,0 +12,2 @@\n+first added line\n+second added line\n"
             b"@@ -40,1 +50,1 @@\n-a removed line\n+a later added line\n")

    def test_added_lines_carry_their_path_and_line_numbers(self):
        lines, inspected = lr.added_lines("upstream/main", run=lambda *_a, **_k: self._DIFF)
        self.assertTrue(inspected)
        self.assertEqual(lines, [("src/app.py", 12, "first added line"),
                                 ("src/app.py", 13, "second added line"),
                                 ("src/app.py", 50, "a later added line")])

    def test_a_removed_line_is_not_scanned(self):
        lines, _ = lr.added_lines("upstream/main", run=lambda *_a, **_k: self._DIFF)
        self.assertNotIn("a removed line", [t for _p, _n, t in lines])

    def test_an_unreadable_diff_is_uninspected_not_empty(self):
        # ([], False) and ([], True) must never collapse: the first is an unknown change, the second a clean
        # one, and a caller that treats them alike narrates cleanliness on something it never read.
        self.assertEqual(lr.added_lines("upstream/main", run=lambda *_a, **_k: None), ([], False))
        self.assertEqual(lr.added_lines("upstream/main", run=lambda *_a, **_k: b""), ([], True))

    def test_undecodable_bytes_cost_a_character_not_the_whole_read(self):
        raw = b"+++ b/x.py\n@@ -0,0 +1 @@\n+caf\xe9 ACME-156\n"
        lines, inspected = lr.added_lines("upstream/main", run=lambda *_a, **_k: raw)
        self.assertTrue(inspected, "a file that is not valid UTF-8 must not collapse the whole diff read")
        self.assertIn("ACME-156", lines[0][2])

    def test_the_read_forbids_rename_detection(self):
        # A rename renders as a header with NO added lines, so a file MOVED into the contribution would carry
        # its references straight past an added-lines scan.
        seen = {}

        def _run(args, checkout=None, **_k):
            seen["args"] = args
            return b""
        lr.added_lines("upstream/main", run=_run)
        self.assertIn("--no-renames", seen["args"])


class TestFindings(unittest.TestCase):
    def test_no_hits_is_no_finding(self):
        self.assertEqual(lr.findings("soft", []), [])

    def test_a_finding_is_soft_and_names_only_the_matched_token(self):
        # The message is published verbatim into a GitHub Issue title and body, so it must never carry the
        # surrounding source line — that line could contain anything the change happened to touch.
        hits = lr.scan(lr.compile_vocabulary({"id_prefixes": ["ACME-"]}),
                       lines=[("src/app.py", 9, "SECRET_TOKEN = 'xyz'  # per ACME-156")])
        fs = lr.findings("hard", hits)          # tier is deliberately not honoured for the scan legs
        self.assertEqual(fs[0]["severity"], "soft")
        self.assertIn("ACME-156", fs[0]["message"])
        self.assertNotIn("SECRET_TOKEN", fs[0]["message"])

    def test_an_implausibly_broad_declaration_says_so_in_its_own_finding(self):
        # Breadth surfaces here — on the bounded diff — rather than in the merge gate, which cannot walk a
        # deployment's whole tree without risking a hard red over that tree's size or encoding.
        vocab = lr.compile_vocabulary({"phrases": ["the"]})
        hits = lr.scan(vocab, lines=[("a.py", n, "the thing") for n in range(40)])
        self.assertIn("too broad", lr.findings("soft", hits)[0]["message"])


class TestCLI(unittest.TestCase):
    def test_demo_runs_green(self):
        self.assertEqual(lr.main(["demo"]), 0)


if __name__ == "__main__":
    unittest.main()
