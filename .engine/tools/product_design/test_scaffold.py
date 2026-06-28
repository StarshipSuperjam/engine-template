#!/usr/bin/env python3
"""Conformance test for the committed product-authoring scaffold
(.engine/modules/product-design/scaffold/).

This is the mechanical "what the engine writes from is what the validator checks" tie behind the maintainer's
scaffold decision: a docs/spec/ tree authored straight from the committed templates must pass BOTH the Slice-1
form check (.engine/tools/product_design/spec_form.py) AND the Slice-4a build-order coverage check
(.engine/tools/product_design/coverage.py). If an edit to the scaffold OR to either checker drifts them apart,
this test goes red — so the templates can never silently fall out of conformance with the rules that validate
the operator's real spec. The test reads the templates from disk (not an embedded copy), so it exercises
exactly the bytes that ship.
"""
from __future__ import annotations
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # .engine/tools on sys.path
from product_design import spec_form  # noqa: E402
from product_design import coverage  # noqa: E402
import validate  # noqa: E402

# The committed scaffold lives beside the module manifest, NOT under a catalogued surface location, so it is
# owned by product-design's `scaffold` provides group without being entitized into the knowledge graph.
_SCAFFOLD_DIR = os.path.join(validate.ROOT, ".engine", "modules", "product-design", "scaffold")
_INDEX_TEMPLATE = os.path.join(_SCAFFOLD_DIR, "spec-index.md")
_CAPABILITY_TEMPLATE = os.path.join(_SCAFFOLD_DIR, "spec-capability.md")
_BUILD_PLAN_TEMPLATE = os.path.join(_SCAFFOLD_DIR, "spec-build-plan.md")


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


class ScaffoldConformanceTests(unittest.TestCase):
    def _tree_from_scaffold(self, *, capability_body=None, build_plan_body=None) -> str:
        """A throwaway repo root whose docs/spec/ is authored straight from the committed scaffold: the index
        template becomes docs/spec/index.md, the capability template becomes docs/spec/spec-capability.md (the
        filename both the index's and the build order's example rows link to), and the build-order template
        becomes docs/spec/build-plan.md — so the authored tree is coherent end to end."""
        d = tempfile.mkdtemp(prefix="engine-scaffold-test-")
        self.addCleanup(shutil.rmtree, d, True)
        spec = os.path.join(d, "docs", "spec")
        os.makedirs(spec)
        with open(os.path.join(spec, "index.md"), "w", encoding="utf-8") as fh:
            fh.write(_read(_INDEX_TEMPLATE))
        with open(os.path.join(spec, "spec-capability.md"), "w", encoding="utf-8") as fh:
            fh.write(capability_body if capability_body is not None else _read(_CAPABILITY_TEMPLATE))
        with open(os.path.join(spec, "build-plan.md"), "w", encoding="utf-8") as fh:
            fh.write(build_plan_body if build_plan_body is not None else _read(_BUILD_PLAN_TEMPLATE))
        return d

    def test_both_scaffold_templates_are_committed(self):
        self.assertTrue(os.path.isfile(_INDEX_TEMPLATE), "the index scaffold template must be committed")
        self.assertTrue(os.path.isfile(_CAPABILITY_TEMPLATE),
                        "the capability scaffold template must be committed")

    def test_a_tree_authored_from_the_scaffold_passes_the_form_check(self):
        # The load-bearing tie: the committed scaffold, used verbatim, yields a spec the Slice-1 checker
        # accepts cleanly. An empty findings list is a clean pass (no hard problem, and no disclosed no-op,
        # because the authored tree is non-empty).
        fs = spec_form.findings("hard", root=self._tree_from_scaffold())
        hard = [f for f in fs if f["severity"] == "hard"]
        self.assertEqual(hard, [],
                         f"a scaffold-authored spec must pass the form check: {[f['message'] for f in hard]}")
        self.assertEqual(fs, [], "a scaffold-authored spec is a clean pass (no no-op note, no findings)")

    def test_the_conformance_check_bites_when_a_required_section_is_removed(self):
        # Falsification: drop the Behavior section the scaffold's capability ships; the checker must fire its
        # named missing-sections finding, so the passing test above is not vacuous.
        broken = _read(_CAPABILITY_TEMPLATE).replace("## Behavior", "## Notes on behavior")
        fs = spec_form.findings("hard", root=self._tree_from_scaffold(capability_body=broken))
        hard = [f for f in fs if f["severity"] == "hard"]
        self.assertTrue(hard, "removing a required section must produce a hard finding")
        self.assertTrue(any("Behavior" in f["message"] for f in hard),
                        f"the finding must name the missing Behavior section: {[f['message'] for f in hard]}")

    def test_the_build_order_template_is_committed(self):
        self.assertTrue(os.path.isfile(_BUILD_PLAN_TEMPLATE),
                        "the build-order scaffold template must be committed")

    def test_a_tree_authored_from_the_scaffold_passes_the_coverage_check(self):
        # The Slice-4a tie: the committed scaffold, used verbatim, also yields a build order the coverage check
        # accepts cleanly. The scaffold capability ships in-progress (not settled) and the build order schedules
        # it, so nothing is orphaned — a clean pass (no hard problem; and not a no-op, because a build order
        # exists). This is the write-from = check-against tie for the build order, paralleling the form-check
        # tie above; an edit to the build-order template OR to coverage.py that drifts them apart goes red here.
        fs = coverage.findings("hard", root=self._tree_from_scaffold())
        self.assertEqual(fs, [], f"a scaffold-authored build order must pass the coverage check: {fs}")

    def test_the_coverage_check_bites_when_the_build_order_link_is_broken(self):
        # Falsification: point the scaffold build order at a document that doesn't exist; coverage must fire its
        # named dangling finding at hard severity, so the passing test above is not vacuous.
        broken = _read(_BUILD_PLAN_TEMPLATE).replace("spec-capability.md", "ghost.md")
        fs = coverage.findings("hard", root=self._tree_from_scaffold(build_plan_body=broken))
        hard = [f for f in fs if f["severity"] == "hard"]
        self.assertTrue(hard, "a broken build-order link must produce a hard finding")
        self.assertTrue(any("doesn't exist" in f["message"] for f in hard),
                        f"the finding must name the missing document: {[f['message'] for f in hard]}")


if __name__ == "__main__":
    unittest.main()
