#!/usr/bin/env python3
"""The kind reconciler — the on:issues CI net that keeps an engine Issue's kind consistent, in both the
GitHub-native label AND the title's `Kind:` prefix.

WHAT THIS IS. Two jobs on every Issue `opened` or `edited`:

  1. NATIVE LABEL (all Issues). Derive the fitting GitHub-native label (`bug`/`enhancement`/`documentation`/
     `question`) and apply it — so browsing/filtering on GitHub is consistent by construction. For an
     engine-authored Issue that carries the authoritative `<!-- engine-kind: … -->` marker the label is
     projected FROM that canonical kind (issue_kind.native_label_for_kind — the single source of truth); for a
     human/legacy Issue with no marker it falls back to parsing the title's prose prefix (native_label_for_title,
     a deliberately WIDER legacy vocabulary that also reads `Docs`/`Question`). It maps only onto the four labels
     GitHub already ships (eADR-0021: the ban is on new labels, not the natives); it mints NOTHING.

  2. TITLE (engine-authored, marker-carrying Issues only — StarshipSuperjam/engine-template#937). Repair the
     title's `Kind:` prefix to match the authoritative marker, so a missing / invented / stale prefix
     (`Architecture: x`, `x`, `Bug: x`) is restored to `<Kind>: x` without operator cleanup. The kind is data
     (the marker); the prefix is presentation.

TITLE-DERIVED, NEVER TITLE-INTERPOLATED. Everything is read from the event JSON at `$GITHUB_EVENT_PATH` — the
title and body are attacker-controllable fields, but they never reach a shell, and the label applied is a fixed
enum while the title written is `issue_kind.render_title`'s normalised output (a JSON body value, never shell/
URL-interpolated). So there is no title→shell and no title→label-value injection.

THE TITLE-WRITE IS DOUBLE-GATED AND FAIL-CLOSED. A title is repaired ONLY when the Issue carries the `engine`
label AND a valid marker parses (engine_kind_or_none). A non-engine Issue (whose title an external user
controls) and an engine Issue with no/garbled marker are BOTH no-ops — the reconciler never guesses a kind. So
a forged marker cannot retitle an arbitrary human Issue: an external user cannot self-apply the `engine` label
that gates this path. RESIDUAL (honest): anyone who can edit an engine Issue's body — the bot author, a
maintainer/triager, OR the Issue's OWN (untrusted) author, who can pre-plant a dormant marker before the
`engine` label exists and have it honoured once a maintainer later adds that label — could steer the marker
parse_kind's last-match rule honours. Bounded: the value is enum-closed, so the worst case is a self-healing
wrong title prefix + native label — a cosmetic effect, never a consent, merge, or gate effect.

GOVERNANCE (disclosed, accepted). This title-write is a new privileged capability gated solely by the one-line
engine_kind_or_none double-gate, and this tool is NOT in weakening_guard's floor — a future edit weakening that
gate would not trip guardrail-weakening. Accepted rather than floored because the capability's blast radius is
cosmetic and self-healing (an in-enum title/label change), not the consent-forging or gate-removing machinery
the floor is reserved for; flooring a frequently-edited CI net would add disproportionate friction. A change
broadening this to any consequential write should reconsider flooring.

LOST-UPDATE SAFE (to a bounded window). The event payload is a frozen snapshot. Before writing a title, the
reconciler RE-READS the Issue live and recomputes from the current title + marker, skipping the write if the
live title is already canonical — so a concurrent human descriptive edit that landed before the re-read is not
reverted. RESIDUAL: GitHub issue-update has no compare-and-set, so an edit landing in the single round trip
BETWEEN the live read and the PATCH is still overwritten; the window is one HTTP round trip and the next
`edited` event re-reconciles, so it is disclosed, not fully closed.

LOOP SAFETY (this net now writes a WATCHED event type — a title edit fires `edited`). Production loop-prevention
rests on GitHub's own rule that an event caused by the workflow's `GITHUB_TOKEN` does NOT trigger another
workflow run — so the reconciler's own title edit does not re-fire this net. Defense-in-depth against a
future maintainer swapping in a PAT (which WOULD re-fire) and against a human's own re-edit: `render_title` is a
normalised fixed point, so a second pass over an already-canonical title issues ZERO writes. The per-Issue
`concurrency` group only serialises runs; it is NOT the loop container.

FAIL CONTRACT (a safety-net, never a gate). No readable event, no issue, or nothing to label or reconcile →
a quiet exit 0 (no-op). Work to do but GITHUB_REPOSITORY/GITHUB_TOKEN unset, or a genuine GitHub API failure →
a non-zero exit so the net's OWN breakage is a visible red run, never a silent pass. A red here gates nothing —
the Issue already exists.

CLI (operator-runnable, falsifiable — the live net is what the workflow invokes):
  uv run --directory .engine -- python tools/issue_kind_label.py demo   # scripted, fake GitHub, self-checks
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import issue_event  # noqa: E402  (the shared on:issues event-parsing boundary)
import issue_gate   # noqa: E402  (the single source for the `engine` label string — stdlib-only, hot-path light)
import issue_kind   # noqa: E402  (the canonical kind vocabulary, marker, and normalised title — single source)
import issue_label_client  # noqa: E402  (the shared per-Issue label + minimal-mutation client)

USER_AGENT = "engine-issue-kind-label"

# Each LEGACY issue-title kind prefix and the GitHub-native label it maps to — the fallback for a human/legacy
# Issue that carries NO engine-kind marker. This is a deliberately WIDER vocabulary than the six canonical kinds
# (issue_kind.KINDS): it also reads the faults telemetry/sessions historically filed (`Bug`/`Engine fault`/
# `Defect`) and the `Docs`/`Question` genres that have a native label but are not change-kinds. Kept as its OWN
# table (not a slice of issue_kind) BECAUSE its range genuinely differs — a two-projection design: the canonical
# kind projects native labels for ENGINE issues (issue_kind.native_label_for_kind), this parses prose for
# NON-engine ones. Do not collapse the two: they would lose the engine/human distinction and the Docs/Question
# range. importing issue_kind here is cheap (a stdlib leaf), so the hot-path concern that keeps release_cut out
# does not apply.
_NATIVE_BY_KIND = {
    "bug": "bug",
    "fix": "bug",
    "engine fault": "bug",
    "defect": "bug",
    "security": "bug",
    "feature": "enhancement",
    "improvement": "enhancement",
    "docs": "documentation",
    "documentation": "documentation",
    "question": "question",
}
# The GitHub-native labels this reconciler may apply — the complete value range, for tests and disclosure.
NATIVE_KIND_LABELS = tuple(dict.fromkeys(_NATIVE_BY_KIND.values()))
# `^<Kind>:` at the very start, case-insensitive. Longer kinds first so `documentation` is never shadowed by
# `docs` (the `:` anchor already prevents it, but ordering makes the intent explicit and robust). Each kind is
# regex-escaped so a multi-word kind (`engine fault`) and any future metacharacter match literally.
_KIND_PREFIX_RE = re.compile(
    r"^\s*(" + "|".join(re.escape(k) for k in sorted(_NATIVE_BY_KIND, key=len, reverse=True)) + r")\s*:",
    re.IGNORECASE,
)


def native_label_for_title(title) -> "str | None":
    """The GitHub-native kind label for an Issue title's leading `Kind:` prefix, or None when the title has no
    mappable kind (e.g. `Migration M3:`, `Delivery wave 2`, a bare descriptive title) — never a guess. The
    LEGACY prose-parse fallback, used for a human/non-engine Issue; an engine Issue with a marker uses the
    canonical projection instead (see native_label_for_issue)."""
    if not isinstance(title, str):
        return None
    m = _KIND_PREFIX_RE.match(title)
    if not m:
        return None
    return _NATIVE_BY_KIND[m.group(1).strip().lower()]


def engine_kind_or_none(issue) -> "str | None":
    """The authoritative canonical kind IFF `issue` is engine-labelled AND its body carries a VALID engine-kind
    marker; else None. The double gate on the title-write path: a non-engine Issue and an engine Issue with a
    missing/garbled/non-enum marker are BOTH None, so neither is ever retitled (never a guess). Reads the
    `engine` label from the shared single source (issue_gate.ENGINE_LABEL) and the marker via issue_kind's
    fail-closed parse (last-match, non-enum → None)."""
    if not isinstance(issue, dict):
        return None
    if issue_gate.ENGINE_LABEL not in issue_event.labels_of(issue):
        return None
    return issue_kind.parse_kind(issue.get("body") or "")


def native_label_for_issue(issue) -> "str | None":
    """The native label to apply to one Issue — projected from the authoritative kind for an engine Issue that
    carries a marker (the single source of truth), else the legacy title-prose parse for a human/pre-marker
    Issue (so a pre-marker engine Issue's label does not regress). None when neither yields a label."""
    kind = engine_kind_or_none(issue)
    if kind is not None:
        return issue_kind.native_label_for_kind(kind)
    return native_label_for_title(issue.get("title") or "")


def apply_kind_label(issue: dict, client) -> str:
    """Ensure the Issue's native kind label is present, idempotently and WITHOUT ever creating it. Returns a
    short action word. Assumes `issue` has a numeric `number`. Any GitHub failure propagates as
    DegradedWriteError (→ a red run). The order matters: derive → already-present? → exists on the repo? → add.
    Skipping a repo-absent label is what keeps this apply-only (never a minter)."""
    native = native_label_for_issue(issue)
    if native is None:
        return "no-kind"
    if native in issue_event.labels_of(issue):
        return "already"
    if not client.label_exists(native):
        return "absent"                      # the repo owner removed this default — skip, never mint
    client.add_label(issue["number"], native)
    return "labelled"


def reconcile_title(issue: dict, client) -> str:
    """Repair one engine-authored Issue's title so its `Kind:` prefix matches the authoritative marker,
    idempotently. Returns a short action word. Double-gated (engine_kind_or_none) — only an engine-labelled
    Issue carrying a valid marker is ever retitled; anything else is a no-op. Lost-update safe: when the frozen
    event snapshot says a repair is needed, it RE-READS the Issue live and recomputes from the current title +
    marker, writing only on a genuine difference — so a concurrent human edit is not reverted. Any GitHub
    failure propagates as DegradedWriteError (→ a red run)."""
    kind = engine_kind_or_none(issue)
    if kind is None:
        return "no-marker"
    snapshot_title = issue.get("title") or ""
    if issue_kind.render_title(kind, snapshot_title) == snapshot_title:
        return "title-canonical"             # the snapshot is already the fixed point — no write, no live read
    # The snapshot says a repair is needed; the event payload is frozen, so re-read live and recompute against
    # the CURRENT title + marker before writing (a concurrent descriptive edit must not be reverted).
    live = client.get_issue(issue["number"])
    live_kind = engine_kind_or_none(live)
    if live_kind is None:
        return "marker-gone"                 # the engine label or marker was removed since the event — never guess
    live_title = live.get("title") or ""
    desired = issue_kind.render_title(live_kind, live_title)
    if desired == live_title:
        return "already-repaired"            # a concurrent edit already made it canonical — nothing to do
    client.edit_title(issue["number"], desired)
    # Disclose the change AFTER it lands (printing before the write would assert a change that a failed PATCH
    # never made). An invisible marker driving a visible retitle is a least-surprise trap, so name the escape
    # hatch plainly: the kind is DATA — change it via the marker, not by fighting the title.
    print(f"kind-reconcile: issue #{issue['number']} retitled {live_title!r} -> {desired!r} (kind marker: "
          f"{live_kind}). The kind is data — to change it, edit the engine-kind marker in the body or re-file "
          f"via the issue helper; removing the `engine` label stops this.")
    return "retitled"


def _run() -> int:
    event = issue_event.load_event()
    if event is None:
        print("kind-label: no readable issue event — nothing to do.")
        return 0
    issue = issue_event.issue_or_none(event)   # scope-free: any Issue with a numeric id (the native-label axis
    if issue is None:                          # is orthogonal to the `engine` label; the title axis gates on it)
        print("kind-label: no issue in the event — no action.")
        return 0
    # Decide whether there is anything to do BEFORE requiring a token (a bare no-op stays quiet, as before): a
    # native label to apply, or an engine+marker Issue whose SNAPSHOT title is not already canonical (a repair
    # may be needed — confirmed live below). An engine+marker Issue whose title already matches its marker AND
    # whose kind projects no native label (Maintenance/Removal) is a TRUE no-op, so it exits 0 without a token,
    # matching reconcile_title's own zero-network short-circuit on an already-canonical snapshot.
    native = native_label_for_issue(issue)
    marker_kind = engine_kind_or_none(issue)
    snapshot_title = issue.get("title") or ""
    title_work = marker_kind is not None and issue_kind.render_title(marker_kind, snapshot_title) != snapshot_title
    if native is None and not title_work:
        print("kind-label: nothing to label and the title already matches any engine-kind marker — no action.")
        return 0
    repo, token = issue_event.resolve_repo_token()
    if not repo or not token:
        print("kind-label: GITHUB_REPOSITORY / GITHUB_TOKEN unset — cannot reach GitHub.", file=sys.stderr)
        return 1
    client = issue_label_client.IssueLabelClient(repo, token, user_agent=USER_AGENT)
    try:
        title_action = reconcile_title(issue, client)   # engine+marker only; a no-op otherwise
        label_action = apply_kind_label(issue, client)
    except issue_label_client.DegradedWriteError as exc:
        print(f"kind-label: a GitHub API call failed — {exc}", file=sys.stderr)
        return 1
    # Each action word rendered so a person scanning an Actions run can read it cold — `absent`/`no-marker` in
    # particular must read as deliberate skips (the repo owner removed a default; the Issue carries no marker),
    # never a fault.
    label_explained = {
        "labelled": "native kind label applied",
        "already": "native kind label already present",
        "absent": "skipped — that native label was removed from this repo, and this tool never creates one",
        "no-kind": "no mappable kind for a native label",
    }
    title_explained = {
        "retitled": "title prefix repaired from the kind marker",
        "title-canonical": "title already matches the kind marker",
        "already-repaired": "title already repaired by a concurrent edit",
        "marker-gone": "skipped — the engine label or marker was removed before the write",
        "no-marker": "not an engine-authored, marker-carrying Issue — title left untouched",
    }
    print(f"kind-label: issue #{issue['number']} -> title: {title_explained[title_action]}; "
          f"label: {label_explained[label_action]}")
    return 0


# ---- the operator-runnable demo (the live net is what the workflow invokes) -------------------

class _FakeGitHub:
    """A scripted GitHub for the demo/tests: records every (method, path, body) and returns canned
    (status, json), so the REAL reconcile logic runs with no network. `label_exists` decides whether the
    repo-label GET reports the native label present; `live_issue` is the dict a live GET on the Issue returns
    (the reconciler's lost-update re-read) — set it to a DIFFERENT title/body than the event snapshot to
    exercise the concurrent-edit path."""

    def __init__(self, *, label_exists: bool = True, live_issue: "dict | None" = None):
        self.calls = []
        self._label_exists = label_exists
        self._live_issue = live_issue

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if "/issues/" in path and path.endswith("/labels"):   # POST: add a label to an issue
            return 200, []
        if "/labels/" in path:                                # GET: does the repo label exist?
            return (200 if self._label_exists else 404), None
        if re.search(r"/issues/\d+$", path):                  # GET (live re-read) or PATCH (title write)
            if method == "PATCH":
                return 200, {}
            return 200, (self._live_issue if self._live_issue is not None else {})
        return 200, None

    def issue_label_adds(self):
        return [c for c in self.calls if c[0] == "POST" and "/issues/" in c[1] and c[1].endswith("/labels")]

    def title_edits(self):
        return [c for c in self.calls if c[0] == "PATCH" and re.search(r"/issues/\d+$", c[1])]


def _client(gh):
    return issue_label_client.IssueLabelClient("o/r", "t", user_agent=USER_AGENT, transport=gh)


def _marker(kind: str) -> str:
    return issue_kind.kind_trailer(kind)


def _demo() -> int:
    """Runs the REAL apply_kind_label / reconcile_title / native_label_for_issue over synthetic issue events
    against a fake GitHub, printing the actual behaviour and self-checking every outcome. Returns 1 on any
    unexpected result (the failure path the in_tool_demo_failure_path floor requires)."""
    ok = True

    def check(desc: str, cond: bool) -> None:
        nonlocal ok
        if not cond:
            ok = False
        print(f"  {desc:70} -> {'OK' if cond else 'UNEXPECTED'}")

    engine = [{"name": issue_gate.ENGINE_LABEL}]
    print("The kind reconciler — what it does for each issue event (real logic, fake GitHub):\n")

    # --- native label (job 1) ---------------------------------------------------------------------
    # 1. a legacy human title, label present on repo, absent on issue -> ONE add
    gh = _FakeGitHub(label_exists=True)
    action = apply_kind_label({"number": 1, "title": "Fix: quote the hook path", "labels": []}, _client(gh))
    check("legacy title, label on repo, not on issue: labelled (one add)",
          action == "labelled" and len(gh.issue_label_adds()) == 1)

    # 2. an engine issue's native label is projected from its MARKER, not the (drifted) title
    gh2 = _FakeGitHub(label_exists=True)
    issue2 = {"number": 2, "title": "Architecture: drifted", "labels": engine, "body": _marker("Fix")}
    check("engine issue: native label projected from the marker (Fix->bug), ignoring the drifted title",
          native_label_for_issue(issue2) == "bug")
    apply_kind_label(issue2, _client(gh2))
    check("  and it is applied (one add)", len(gh2.issue_label_adds()) == 1)

    # 3. an unmappable title with no marker -> no native label
    check("unmappable title, no marker: no native label",
          native_label_for_issue({"number": 3, "title": "Migration M3: x", "labels": []}) is None)

    # --- title reconcile (job 2) ------------------------------------------------------------------
    # 4. engine + marker, invented prefix -> retitled to the canonical prefix (live re-read agrees)
    live4 = {"number": 4, "title": "Architecture: example", "labels": engine, "body": _marker("Improvement")}
    gh4 = _FakeGitHub(live_issue=live4)
    action4 = reconcile_title(dict(live4), _client(gh4))
    check("engine+marker, invented prefix 'Architecture:' -> retitled 'Improvement: example'",
          action4 == "retitled" and gh4.title_edits() and gh4.title_edits()[0][2] == {"title": "Improvement: example"})

    # 5. already-canonical -> a pure no-op (no live read, no write): the fixed point
    gh5 = _FakeGitHub()
    action5 = reconcile_title(
        {"number": 5, "title": "Improvement: example", "labels": engine, "body": _marker("Improvement")}, _client(gh5))
    check("engine+marker, already canonical: no write and no live read (fixed point)",
          action5 == "title-canonical" and gh5.calls == [])

    # 6. no marker -> title untouched (never guess), even with a kind-shaped title
    gh6 = _FakeGitHub()
    action6 = reconcile_title({"number": 6, "title": "Bug: something", "labels": engine, "body": "no marker"}, _client(gh6))
    check("engine, NO marker: title untouched (never guess)", action6 == "no-marker" and gh6.title_edits() == [])

    # 7. non-engine issue with a (forged) marker -> title untouched (the engine-label gate holds)
    gh7 = _FakeGitHub()
    action7 = reconcile_title(
        {"number": 7, "title": "whatever", "labels": [{"name": "bug"}], "body": _marker("Security")}, _client(gh7))
    check("NON-engine issue + forged marker: title untouched (double gate fail-closed)",
          action7 == "no-marker" and gh7.title_edits() == [])

    # 8. lost update: the snapshot needs a repair, but a concurrent human edit already made it canonical LIVE
    live8 = {"number": 8, "title": "Improvement: a better example", "labels": engine, "body": _marker("Improvement")}
    gh8 = _FakeGitHub(live_issue=live8)
    action8 = reconcile_title(
        {"number": 8, "title": "Architecture: example", "labels": engine, "body": _marker("Improvement")}, _client(gh8))
    check("lost-update: live title already canonical -> no write (concurrent edit not reverted)",
          action8 == "already-repaired" and gh8.title_edits() == [])

    # 9. the mapping itself — the legacy prose fallback, spot-checked across kinds + edge cases
    cases = {
        "Fix: x": "bug", "Bug: x": "bug", "Engine fault: x": "bug", "Defect: x": "bug", "Security: x": "bug",
        "Feature: x": "enhancement", "Improvement: x": "enhancement",
        "Docs: x": "documentation", "Documentation: x": "documentation", "Question: x": "question",
        "Maintenance: x": None, "Delivery wave 2": None, "no prefix at all": None, "": None,
    }
    for title, expected in cases.items():
        check(f"native_label_for_title({title!r}) == {expected!r}", native_label_for_title(title) == expected)

    print(f"\n  Native labels this reconciler may apply (never mints): {', '.join(NATIVE_KIND_LABELS)}")
    print(f"  Canonical kinds it repairs titles to (from the marker): {', '.join(issue_kind.KINDS)}")

    if not ok:
        print("\nDEMO UNEXPECTED: an outcome did not match the reconciler's contract.", file=sys.stderr)
        return 1
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    return _run()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
