"""Pure PR-body composer for the Build coordinator's `contract` verbs.

WHAT THIS IS. The coordinator composes the pull-request draft body from one typed claim
(`pr-body-claim.v1` — judgment-bearing narrative and session-only observations) plus evidence the
coordinator computes from observed facts. This module is the PURE half: it validates a claim and
assembles a complete body from the claim and a supplied `evidence` mapping. It performs no I/O beyond
reading the claim schema and (via `release_cut.template_preamble`) the committed PR template, makes no
network call, and touches no Build state — so it is unit-testable in isolation. The coordinator side
(`build_coordinator.py`'s `contract` verbs) computes the evidence, drives the safe live-apply loop, and
records state; none of that lives here.

THE PARTITION (why the split is where it is). Deterministic facts are the coordinator's to compute and
appear in `evidence`; the claim carries only what needs judgment or is observable solely by the session.
So the composer NEVER derives a fact from the claim that the coordinator can observe: the consent
preamble, the change profile, spec-derived acceptance steps, validation results, reviewer-disagreement
lines, reviewed/final commits and divergence, index-regeneration facts, and the guardrail-touched file
set all arrive through `evidence`, already computed. The composer's job is assembly, not authorship.

THE EVIDENCE CONTRACT. `compose(claim, evidence)` reads these keys (all coordinator-supplied):

  preamble            str        OPTIONAL — the consent blockquote. The coordinator does not supply it; the
                                 composer lifts it from the committed template via release_cut.template_preamble().
  closes              [int]      the reconciled final set of issues this PR closes — the coordinator
                                 merges the claim's linkage with any durable Build Issue and reconciles
                                 against live GitHub. Rendered as one `Closes #N` line at the TOP.
                                 (Part-of issues come from the claim's linkage, rendered as one
                                 `Part of #N` line each, also at the top — never comma-separated.)
  change_profile      str        scope_profile.render(...) block, pasted verbatim into Scope
  validation_results  str        rendered validation facts (suite, pass/fail, counts, commit,
                                 log digests) — coordinator-computed, no machine-local log paths
  index_regen         str        computed index-regeneration disclosure ("N generated index file(s) changed;
                                 only generated paths"), or "" when nothing regenerated (BO-24)
  spec_steps          str        multi-document spec-derived acceptance steps (two groups), or the
                                 honest no-spec disclosure — rendered by spec_referent, never here
  review_coverage     str        depth and the passes that ran, rendered from coordinator evidence
  code_execution_line str        the code-execution disclosure (BO-41), computed from the review receipts
  disagreement_lines  [str]      required reviewer-disagreement lines, verbatim from the coordinator
  drift_line          str        the reviewed->submitted commit/divergence sentence, coordinator-computed
  close_linkage_lines [str]      advisory close-linkage lines to fold into Review (apply's fixed-point pass)
  composition_marker  str        the hidden marker carrying the claim digest and final commit
  preserved_blocks    [str]      valid marker blocks already on the draft (plan / handoff / build-id)
                                 to carry through unchanged

(The guardrail-touch disclosure is the claim's `risk.guardrail_note`, and an open fail-open finding is a
claim `validation.caveats` entry — both judgment, so neither is coordinator-supplied evidence.)

Every string arrives ready to place; the composer owns only ordering, headings, and the section shape.
"""
from __future__ import annotations

import json
import os
import sys

# Reuse the single-homed PR-body formatter (a bold summary, its bullets, an *Impact:* line) and the
# consent-preamble lift. Imported LAZILY inside the functions that need them: a top-level import of
# release_cut would pull the whole release-production / module-management subsystem (module_manager,
# bootstrap, wiring, ...) into the per-PR Build coordinator's import graph for two pure helpers.

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
_CLAIM_SCHEMA_PATH = os.path.join(ROOT, ".engine", "schemas", "pr-body-claim.v1.json")

# The nine required level-2 sections, in the order the completeness gate enforces. Behaviors is a
# level-3 subsection of Scope, not a tenth section (see `pr-body-completeness.json` / the template).
HEADING_ORDER = [
    "Purpose", "Scope", "Out of scope", "Risk", "Validation",
    "Review", "Demonstration", "Files of interest", "AI involvement",
]


class ContractError(Exception):
    """A claim or evidence problem the caller must surface with a precise remediation."""


def _load_schema() -> dict:
    with open(_CLAIM_SCHEMA_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def fillable_template() -> dict:
    """The empty claim shape the `contract template` verb emits. Every judgment slot is `null` and every
    list is empty, so this skeleton does NOT validate — running it through `validate_claim` names each
    unfilled slot by path. It is a fill-me guide, never a valid claim: ordinary schema validation catches an
    unfilled field without any substring grading of prose."""
    return {
        "schema_version": "pr-body-claim.v1",
        "release_impact": None,   # the null fails enum validation, so this versioning slot must be chosen
        "linkage": {"closes": [], "part_of": []},
        "purpose": {"thesis": None, "problem": None, "mechanism": [], "impact": None},
        "scope": {"summary": None, "items": [], "impact": None},
        "out_of_scope": {"summary": None, "items": [], "impact": None},
        "risk": {"items": [], "guardrail_note": None, "accepted_residual": [], "impact": None},
        "behaviors": {"observable": True, "entries": []},
        "demonstration": {"kind": "runnable", "command": None, "pass_signal": None, "fail_signal": None},
        "validation": {"summary": None, "caveats": [], "live_helpers": {"all_available": True, "unavailable": []},
                       "impact": None},
        "review": {"summary": None, "loop_narrative": [], "material_divergence": False, "finding_summaries": [],
                   "impact": None},
        "files_of_interest": {"items": [], "impact": None},
        "ai_involvement": {"tools": [], "operator_decisions": [], "judgment_split": None, "impact": None},
    }


def load_claim(path: str) -> dict:
    """Read and validate a claim file against `pr-body-claim.v1`, plus the two cross-field rules JSON
    Schema cannot express (linkage uniqueness/disjointness). Raises ContractError with a plain message."""
    try:
        with open(path, encoding="utf-8") as fh:
            claim = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ContractError(f"could not read the claim file {path}: {exc}") from exc
    validate_claim(claim)
    return claim


def validate_claim(claim: dict) -> None:
    """Validate an already-parsed claim. Schema first, then the linkage disjointness cross-check."""
    from jsonschema import Draft202012Validator

    schema = _load_schema()
    errors = sorted(Draft202012Validator(schema).iter_errors(claim), key=lambda e: list(e.path))
    if errors:
        # List EVERY problem, not just the first — a session filling the claim should fix them in one pass,
        # not discover them one round-trip at a time. Remediation is neutral: a problem may be an unfilled
        # slot OR a malformed value (e.g. an embedded newline), so it never assumes "fill the null slots".
        lines = []
        for e in errors:
            where = "/".join(str(p) for p in e.path) or "(root)"
            lines.append(f"  - {where}: {e.message}")
        raise ContractError(
            f"the claim does not satisfy pr-body-claim.v1 ({len(errors)} problem(s)); fix each below "
            f"(fill an empty slot, or correct a malformed value):\n" + "\n".join(lines)
        )
    closes = set(claim["linkage"]["closes"])
    part_of = set(claim["linkage"]["part_of"])
    overlap = closes & part_of
    if overlap:
        nums = ", ".join(f"#{n}" for n in sorted(overlap))
        raise ContractError(
            f"linkage.closes and linkage.part_of overlap on {nums}: an issue is either closed by this "
            f"PR or only part-of it, never both — decide which and remove it from the other list"
        )


def assert_template_matches_gate(sections: list) -> None:
    """Fail loudly if the template's section order (as the composer will emit it) disagrees with the
    completeness rule's declared sections. The gate is the source of truth for what a body must carry;
    the composer must never render a body the gate would then reject for a heading mismatch."""
    rule_path = os.path.join(ROOT, ".engine", "check", "pr-body-completeness.json")
    with open(rule_path, encoding="utf-8") as fh:
        rule = json.load(fh)
    declared = rule.get("params", {}).get("sections") or rule.get("sections")
    if declared != sections:
        raise ContractError(
            "the composer's section order disagrees with the pr-body-completeness gate "
            f"(composer={sections}, gate={declared}); reconcile before composing"
        )


def _preamble(evidence: dict) -> str:
    """The consent blockquote. Prefer the evidence-supplied value (already lifted by the coordinator);
    fall back to lifting it here so the composer is usable standalone in tests."""
    pre = evidence.get("preamble")
    if pre:
        return pre
    sys.path.insert(0, os.path.join(ROOT, ".engine", "tools"))
    import release_cut

    return release_cut.template_preamble()


def _section(header: str, summary: str, body_lines: list, impact: str) -> list:
    """Delegate to the single-homed formatter so every section reads in the one template shape."""
    sys.path.insert(0, os.path.join(ROOT, ".engine", "tools"))
    import release_cut

    return release_cut.pr_section(header, summary, body_lines, impact)


def _behaviors_block(claim: dict) -> list:
    """The level-3 Behaviors subsection, placed inside Scope after its *Impact:* line. Opens with a bold
    intro line (matching the exemplars) so the subsection reads as a section, not a bare list."""
    b = claim["behaviors"]
    if b["observable"]:
        out = ["### Behaviors", "", "**The capabilities this change delivers, each with the test that exercises it.**", ""]
        for entry in b["entries"]:
            tests = ", ".join(f"`{t}`" for t in entry["tests"])
            line = f"- {entry['claim']} — {tests}"
            if entry.get("regression_lock"):
                line += f" ({entry['regression_lock']})"
            out.append(line)
    else:
        out = ["### Behaviors", "",
               f"**Nothing here is observable behaviour** — {b['none_observable_reason']}"]
    out.append("")
    return out


def _bullets(items: list) -> list:
    return [f"- {it}" for it in items]


def compose(claim: dict, evidence: dict) -> str:
    """Assemble the complete PR body from a validated claim and coordinator-computed evidence.

    Validates the claim, cross-checks the section order against the live gate, then renders the consent
    preamble, closing declarations, all nine sections in gate order (Behaviors nested under Scope), and
    reattaches any preserved marker blocks and the hidden composition marker. Returns the body string;
    the caller applies it to the draft PR and re-runs preflight."""
    validate_claim(claim)
    assert_template_matches_gate(HEADING_ORDER)

    lines: list = [_preamble(evidence), ""]

    # Linkage sits at the TOP, one declaration per line, never comma-separated (a comma-listed close links
    # only its first issue). Closes first, then Part of. Built from integer lists so a comma-run is impossible;
    # `closes` is the coordinator's reconciled final set, `part_of` comes straight from the claim.
    linkage = [f"Closes #{n}" for n in evidence.get("closes", [])]
    linkage += [f"Part of #{n}" for n in claim["linkage"]["part_of"]]
    if linkage:
        lines += linkage + [""]

    # 1. Purpose
    p = claim["purpose"]
    lines += _section("Purpose", p["thesis"], [p["problem"], "", *_bullets(p["mechanism"])], p["impact"])

    # 2. Scope (+ change profile, + Behaviors). Part-of dependencies render as reasoned bullets in
    # Out of scope (below), where the exemplars place them and the close-linkage preflight reads them.
    s = claim["scope"]
    scope_body = _bullets(s["items"])
    if evidence.get("change_profile"):
        scope_body += ["", evidence["change_profile"]]
    lines += _section("Scope", s["summary"], scope_body, s["impact"])
    lines += _behaviors_block(claim)

    # 3. Out of scope (reasoned exclusions + any Part-of dependencies)
    o = claim["out_of_scope"]
    oos_body = []
    for it in o["items"]:
        line = f"- {it['item']} — {it['reason']}"
        refs = []
        if it.get("tracked_as"):
            refs.append(f"tracked as {it['tracked_as']}")
        if it.get("deferred_by"):
            refs.append(f"deferred by {it['deferred_by']}")
        if refs:
            line += f" ({'; '.join(refs)})"
        oos_body.append(line)
    lines += _section("Out of scope", o["summary"], oos_body, o["impact"])

    # 4. Risk — each item a bold lead then the bound, matching the exemplars' Risk shape.
    r = claim["risk"]
    risk_body = []
    for it in r["items"]:
        lead = it["risk"].rstrip(".")
        if it.get("most_sensitive"):
            lead += " (the most safety-sensitive edit)"
        risk_body.append(f"- **{lead}.** {it['bound']}")
    if r.get("guardrail_note"):
        risk_body.append(f"- **Guardrail disclosure.** {r['guardrail_note']}")
    for res in r.get("accepted_residual", []):
        risk_body.append(
            f"- **Accepted residual (`{res['finding_id']}`, operator decision {res['operator_decision_date']}).** "
            f"{res['rationale']}"
        )
    lines += _section("Risk", _risk_summary(r), risk_body, r["impact"])

    # 5. Validation — the mechanical results are coordinator-computed; the framing (summary + Impact) and any
    # honest caveats (a fail-open finding among them) are the claim's judgment.
    v = claim["validation"]
    val_body = []
    if evidence.get("validation_results"):
        val_body.append(evidence["validation_results"])
    for caveat in v["caveats"]:
        val_body.append(f"- Caveat: {caveat}")
    val_body.append(_live_helpers_line(v["live_helpers"]))
    if evidence.get("index_regen"):
        val_body.append(f"- {evidence['index_regen']}")
    lines += _section("Validation", v["summary"], val_body, v["impact"])

    # 6. Review — bold-led entries (coverage, the review→repair→re-review story, findings, disagreements,
    # divergence) then the spec-derived acceptance steps, matching the exemplars' richest section.
    rev = claim["review"]
    review_body = []
    if evidence.get("review_coverage"):
        review_body.append(f"- **Coverage.** {evidence['review_coverage']}")
    if evidence.get("code_execution_line"):
        review_body.append(f"- **Code execution.** With this PR, {evidence['code_execution_line']}.")
    for entry in rev["loop_narrative"]:
        review_body.append(f"- {entry}")
    for fs in rev["finding_summaries"]:
        line = f"- **Finding `{fs['id']}`.** {fs['operator_summary']}"
        if fs.get("public_reference"):
            line += f" ({fs['public_reference']})"
        review_body.append(line)
    for dl in evidence.get("disagreement_lines", []):
        review_body.append(dl)
    # Post-approval assumption resolutions the operator must meet at merge: a premise authored 'unresolved'
    # that the orchestrator later self-attested as resolved WITHOUT a plan revision or re-review
    # (StarshipSuperjam/engine-template#1014). Disclosed verbatim so a 'verified' disposition can never
    # vanish silently the way a plan-authored 'verified' does.
    for ar in evidence.get("assumption_resolutions", []):
        review_body.append(f"- **Assumption resolved after approval.** {ar}")
    if evidence.get("drift_line"):
        review_body.append(f"- **Reviewed vs submitted.** {evidence['drift_line']}")
    for cl in evidence.get("close_linkage_lines", []):
        review_body.append(f"- {cl}")
    if evidence.get("spec_steps"):
        review_body += ["", "### Spec-derived acceptance steps", "", evidence["spec_steps"]]
    lines += _section("Review", rev["summary"], review_body, rev["impact"])

    # 7. Demonstration
    lines += _section("Demonstration", _demo_summary(claim["demonstration"]),
                      _demo_body(claim["demonstration"]), _demo_impact(claim["demonstration"]))

    # 8. Files of interest
    f = claim["files_of_interest"]
    files_body = [f"- `{it['path']}` — {it['role']}" for it in f["items"]]
    lines += _section("Files of interest", "The paths that most determine this change.", files_body, f["impact"])

    # 9. AI involvement
    ai = claim["ai_involvement"]
    ai_body = [f"- {t['tool']} ({t['model']}) — {t['role']}" for t in ai["tools"]]
    for dec in ai.get("operator_decisions", []):
        when = f", {dec['decision_date']}" if dec.get("decision_date") else ""
        ai_body.append(f"- Operator decision{when}: {dec['summary']}")
    ai_body.append(f"- {ai['judgment_split']}")
    lines += _section("AI involvement", "How this change was produced and who decided what.", ai_body, ai["impact"])

    body = "\n".join(lines).rstrip() + "\n"

    # The declared release impact (StarshipSuperjam/engine-template#942): a VISIBLE operator-readable line so a reviewer of this pull
    # request sees the declaration without reading an HTML comment, plus the machine marker the release action's
    # fold and the pr-release-impact CI check read. The session supplies only the enum value (claim.release_impact,
    # schema-required); the renderer owns both projections — exactly one marker, so the check's "exactly one" holds.
    impact = claim.get("release_impact")
    if impact:
        import release_impact  # stdlib-only leaf; local import matches this module's lazy-import discipline
        body += "\n*" + release_impact.impact_line(impact) + "*\n"
        body += "\n" + release_impact.impact_trailer(impact) + "\n"

    marker = evidence.get("composition_marker")
    if marker:
        body += "\n" + marker + "\n"
    for block in evidence.get("preserved_blocks", []):
        body += "\n" + block + "\n"
    return body


def _risk_summary(r: dict) -> str:
    n = len(r["items"])
    return f"{n} risk{'s' if n != 1 else ''}, ranked, each with the bound that contains it."


def _live_helpers_line(lh: dict) -> str:
    if lh.get("all_available"):
        return "- The engine's live helpers answered this session."
    parts = []
    for u in lh.get("unavailable", []):
        parts.append(f"{u['helper']} ({u['diagnosis']})")
    named = "; ".join(parts) or "one or more helpers"
    return (f"- A live helper was unavailable, so this change was authored on the committed-file "
            f"fallback: {named}. That area was not verified against live state.")


def _demo_summary(demo: dict) -> str:
    if demo["kind"] == "runnable":
        return "A step you can run yourself that drives the changed surface and can genuinely fail."
    if demo["kind"] == "spec-derived":
        return "The spec-derived acceptance steps in Review drive this change directly."
    return "This change has no observable behaviour to demonstrate."


def _demo_body(demo: dict) -> list:
    if demo["kind"] == "runnable":
        body = [f"- Run: `{demo['command']}`",
                f"- It PASSES when: {demo['pass_signal']}",
                f"- It FAILS when: {demo['fail_signal']}"]
        if demo.get("real_output"):
            body += ["", "```", demo["real_output"], "```"]
        return body
    if demo["kind"] == "spec-derived":
        return ["- See the spec-derived acceptance steps under Review."]
    reasons = {
        "docs-only": "a documentation-only change",
        "dependency": "a dependency change with no behaviour of its own",
        "behaviour-preserving-refactor": "a behaviour-preserving refactor",
        "release-plumbing": "release plumbing",
    }
    return [f"- No demonstration: {reasons[demo['reason']]}."]


def _demo_impact(demo: dict) -> str:
    if demo["kind"] == "none":
        return "Nothing observable to run; correctness rests on review and your read at merge."
    return "Run it to watch the change work — an unrun step is a promise, not proof."
