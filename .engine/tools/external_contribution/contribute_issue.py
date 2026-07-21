#!/usr/bin/env python3
"""Cross-repo issue contribution — file an Issue into a target repo, following ITS own conventions.

WHAT IT DOES. The issue-shaped sibling of `submit.py`. When the Engine files an Issue into a repo it contributes
to (an open-source upstream, or engine-template reached by a fork-native deployment escalating an engine fix), this prepares and — on the
operator's explicit go-ahead — opens that Issue, carrying the TARGET repo's own issue-title convention rather
than imposing the Engine's own. GitHub applies an issue template's `title:` prefix (e.g. `Bug: `) only on the
web "New issue" form; every programmatic path (`gh issue create --title`, the REST `POST …/issues`) bypasses it,
so a contributed Issue would otherwise land with no prefix at all.

READ THE TARGET REMOTELY, NOT THE RUNNING TREE (the load-bearing correctness point). `gh issue create --repo X`
is a REMOTE act — it needs no local checkout of X. So the target's conventions are read REMOTELY too, from the
target repo's own committed files via `gh api repos/{target}/contents/.github/ISSUE_TEMPLATE`, NOT from the
Engine's own working tree. This deliberately differs from `submit.py`, which reads the local tree only because
it runs *inside* a fork checkout of its target; whenever the running tree is NOT the target itself — a fork
carrying the Engine's own overlaid templates, or any separate-checkout topology — a local-tree read would read
the wrong templates and impose them on the target, the exact engine/product-wall inversion this tool exists to
prevent. Reading the target remotely makes the tool correct regardless of where the Engine runs.

FOLLOW THE HOST, NEVER IMPOSE THE ENGINE'S OWN (the engine/product wall). The title prefix and the body shape
are the TARGET repo's, read from its committed issue templates — NOT the Engine's own engine-domain body
contract (`issue_author.py`), whose "the engine opened this item itself" framing is right for the Engine's own
health Issues in its own repo and WRONG for an Issue authored on the operator's behalf into someone else's
project. When the target defines no issue templates — or the chosen template carries no `title:` prefix (GitHub
treats `title:` as optional) — there is no heading to add, so the Issue is filed with a plain title, and the
narration says exactly that rather than claiming a heading it did not apply.

NEVER GUESS THE KIND (surface, don't auto-interpret). A target commonly defines several templates (bug /
feature / …), each with its own prefix. The engine does not guess which one a contribution is: when the
requested `kind` does not resolve to one of the target's templates, this returns the available kinds for the
operator to choose — the same "surfaced, never auto-interpreted" posture `submit.py` takes with the host's
CONTRIBUTING file.

THE HUMAN GATE (the outward act is never the engine's alone). Opening an Issue on another repo is outward-facing
and creates a public record under the operator's identity, so `contribute_issue()` PREPARES everything — the
prefixed title and the host-followed body — and stops. It files the Issue ONLY on an explicit affirmative
decision (`confirm=True`); without it, the prepared Issue is returned for the operator to approve. The
read-and-propose posture at the engine/product wall: the engine reads-and-proposes; the human authorizes.

DEGRADATION (never stranded, never a duplicate). A non-zero `gh issue create` means the Issue was NOT created,
so nothing is lost: the prepared title and body are returned as a DRAFT the operator can file themselves, and
the stall is best-effort traced via telemetry into the operator's OWN repo (never the target). Conversely a
zero-exit `gh` result means the Issue WAS created; even if the returned URL can't be captured, the tool reports
it filed (never "nothing submitted"), so the operator is never nudged into filing a duplicate public Issue.

NETWORK BOUNDARIES ARE INJECTED (matching submit.py). Every network boundary is injectable — the `gh` transport
(`gh_run`, used for BOTH the template fetch and the filing) and the telemetry boundary (`github`) — so the whole
deterministic surface (template detection, kind resolution, title/body assembly, the confirm gate, degradation)
is covered offline by `test_contribute_issue.py` and the falsifiable `demo`. The real `gh issue create` against
the target is the only boundary that acts on the network; it runs behind `gh_run` the first time an operator
actually files, the way any released feature runs its live path the first time it is used.

CONTRACT. This is an operation tool, not a `custom/script` check — it is invoked by the engine/operator (and
narrated by the `external-contribution-issue` runbook), never by the validator. `demo` runs a falsifiable
self-check and prints the real operator-facing narration.
"""
from __future__ import annotations

import json
import os
import sys

# Make the parent `.engine/tools/` importable (`validate`, `telemetry`) — the submit.py idiom, whether imported
# as `external_contribution.contribute_issue` or run directly as a script.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
for _p in (_HERE, _PARENT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import validate  # noqa: E402 — the shared body reader (_body_without_frontmatter)
import telemetry  # noqa: E402 — promote_finding (the stalled-contribution trace), utc_now, GitHubIssues


# ---- target issue-template detection, read REMOTELY from the target (follow the host's conventions) --------

# The committed locations a GitHub project keeps its issue templates. GitHub itself reads
# `.github/ISSUE_TEMPLATE/`; the `docs/` location mirrors submit.py's PR tuple-scan for parity. First present
# DIRECTORY wins; within it every `.md` template is read (a `config.yml` and any non-markdown are skipped —
# YAML issue *forms* are a separate shape, out of scope here).
_ISSUE_TEMPLATE_DIRS = (".github/ISSUE_TEMPLATE", "docs/ISSUE_TEMPLATE")


def _frontmatter_and_body(text: str) -> tuple[dict | None, str]:
    """Parse a template's fetched TEXT into (frontmatter_dict, body_text). Mirrors `validate.frontmatter`'s
    parse — split on the first two `---` fences, `yaml.safe_load` ONLY (never arbitrary object construction
    from an untrusted target's template) — but over text rather than a path, because the template is fetched
    from the target over the network, not read from a local file. Returns (None, "") when the frontmatter is
    malformed, so one bad template is skipped, never crashing the scan."""
    if not text.startswith("---"):
        return {}, validate._body_without_frontmatter(text)
    parts = text.split("---", 2)          # maxsplit=2: a later `---` in the body stays in the body
    if len(parts) < 3:
        return {}, text
    try:
        import yaml                        # lazy, mirroring validate.frontmatter's tool-runtime dep
        data = yaml.safe_load(parts[1])
    except Exception:  # noqa: BLE001 — malformed YAML frontmatter → skip this template, never crash the scan
        return None, ""
    return (data if isinstance(data, dict) else {}), validate._body_without_frontmatter(text)


def detect_upstream_issue_templates(upstream_repo: str, *, gh_run=None) -> list:
    """The TARGET repo's own issue templates, fetched REMOTELY from that repo — a list of kind descriptors, or
    `[]` when the target defines none / cannot be read. Each descriptor is
    `{key, name, about, title_prefix, body_text}`: `key` is the template's filename stem (e.g. `bug`), the
    lowercased handle a caller resolves a `kind` against; `title_prefix` is the frontmatter `title:` verbatim
    (e.g. `"Bug: "`, trailing space and all — GitHub pre-fills it verbatim, so the engine carries it verbatim).

    Reads via `gh api repos/{upstream_repo}/contents/{dir}` (the directory listing) then the raw content of each
    `.md` file — NOT the running engine's own checkout, so it follows the TARGET's conventions regardless of
    where the Engine runs. Scans the first present template DIRECTORY (`.github/ISSUE_TEMPLATE/`, then a `docs/`
    fallback). `gh_run(args) -> (rc, stdout, stderr)` is injectable so tests and the demo run fully offline; a
    404 / unreachable directory or file degrades to skipping it (never a crash, never a false template set)."""
    gh = gh_run or _run_gh
    for d in _ISSUE_TEMPLATE_DIRS:
        rc, out, _err = gh(["api", f"repos/{upstream_repo}/contents/{d}"])
        if rc != 0 or not out:
            continue  # directory absent (404) or unreadable → try the next candidate location
        try:
            entries = json.loads(out)
        except (ValueError, TypeError):
            continue  # unparseable listing → not a usable template directory
        if not isinstance(entries, list):
            continue  # a file (not a dir) at that path, or an error object → not a template directory
        templates = []
        for entry in sorted(entries, key=lambda e: (e or {}).get("name", "") if isinstance(e, dict) else ""):
            if not isinstance(entry, dict):
                continue
            name = entry.get("name") or ""
            # `name` is third-party (the target's own contents listing). GitHub returns basenames, but defend
            # it ourselves rather than lean on that contract: skip anything that is not a plain basename before
            # it is interpolated into a gh api path (no path separator, no traversal, no leading dash).
            if name != os.path.basename(name) or ".." in name or name.startswith("-"):
                continue
            if entry.get("type") != "file" or not name.lower().endswith(".md"):
                continue  # config.yml / YAML issue forms / subdirectories are not classic templates → skip
            rc2, raw, _e2 = gh(["api", "-H", "Accept: application/vnd.github.raw",
                                f"repos/{upstream_repo}/contents/{d}/{name}"])
            if rc2 != 0 or not raw:
                continue  # a file we couldn't fetch → skip it, never crash the scan
            fm, body = _frontmatter_and_body(raw)
            if fm is None:
                continue  # malformed frontmatter → skip this one template
            prefix = fm.get("title")
            templates.append({
                "key": name[:-3].lower(),  # filename stem, lowercased — the handle a `kind` resolves against
                "name": (fm.get("name") or name[:-3]),
                "about": (fm.get("about") or ""),
                "title_prefix": prefix if isinstance(prefix, str) else "",
                "body_text": body,
            })
        return templates  # the first present directory is the convention (empty list if it held no templates)
    return []


def resolve_kind(kind: str | None, templates: list) -> dict | None:
    """The target template a requested `kind` names, or None when it does not resolve (so the caller surfaces
    the choices rather than guessing). Matches case-insensitively against a template's `key` (its filename
    stem) first, then its human `name`, tolerating a trailing `:`/spaces a caller might pass (`"Bug:"` →
    `bug`)."""
    if not kind or not templates:
        return None
    want = kind.strip().rstrip(":").strip().lower()
    if not want:
        return None
    for t in templates:
        if t["key"] == want:
            return t
    for t in templates:
        if str(t.get("name", "")).strip().lower() == want:
            return t
    return None


def build_issue_title(*, kind: str | None, summary: str, templates: list) -> tuple[str, dict | None]:
    """The issue title, carrying the target's own prefix — `(title, matched_template_or_None)`.

      - `kind` resolves to a target template  → `f"{title_prefix}{summary}"` (the prefix verbatim, as GitHub
        pre-fills it — which for a template with no `title:` is the empty string, i.e. a plain title), and the
        matched template.
      - `kind` does not resolve (unknown/absent), templates exist → `(summary, None)`; the caller surfaces the
        available kinds and does NOT file (never guess a kind).
      - the target defines no templates → `(summary, None)`; there is no convention to follow, so a plain title.
    """
    matched = resolve_kind(kind, templates)
    summary = summary.strip()
    if matched is None:
        return summary, None
    return f"{matched['title_prefix']}{summary}", matched


def build_issue_body(*, summary: str, template_text: str | None = None) -> str:
    """The contributed Issue's body. When the chosen template has a body, follow the host's form: lead with the
    plain one-line summary, then carry the target template's body for completion. When there is none, a plain
    summary. NEVER the Engine's engine-domain body contract (issue_author) — this Issue is authored on the
    operator's behalf into another repo, not opened by the Engine about itself."""
    summary = summary.strip()
    body_text = (template_text or "").strip()
    if body_text:
        return f"{summary}\n\n{body_text}"
    return summary


# ---- operator-facing narration (peer voice; plain language, no engine jargon) -----------------

def _needs_summary_narration(upstream_repo: str) -> str:
    return (
        f"Before I can prepare anything for {upstream_repo}, I need a sentence saying what the issue is — tell "
        "me what it's about and I'll write it up for you to review."
    )


def _kind_choice_narration(upstream_repo: str, templates: list) -> str:
    """When the kind didn't resolve: name the target's own kinds so the operator can pick one — never guess.
    Names the human-readable label first (what GitHub shows in its own issue chooser), with the short handle to
    say. Framed as 'prepare', not 'file' — naming a kind still stops at the human confirm gate."""
    lines = [
        f"{upstream_repo} has its own kinds of issue, and I don't want to guess which one this is. Tell me "
        "which to use and I'll prepare it under that kind's heading — you'll still confirm before anything is "
        "filed:",
    ]
    for t in templates:
        about = f" — {t['about']}" if t.get("about") else ""
        lines.append(f"  • {t['name']} (say “{t['key']}”){about}")
    return "\n".join(lines)


def _prepared_narration(upstream_repo: str, title: str, prefixed: bool) -> str:
    convention = ("It carries that project's own issue heading" if prefixed
                  else "There's no heading for this kind on that project, so it's a plain title")
    return (
        f"I've prepared the issue for {upstream_repo}:\n  “{title}”\n{convention}. I won't open it until you "
        "say so — filing an issue on a project you're contributing to is your call. Say the word and I'll file it."
    )


def _filed_narration(upstream_repo: str, url: str | None) -> str:
    if url:
        return (
            f"I've filed the issue on {upstream_repo}. It's a public record on their project now; the "
            f"maintainers decide what happens with it from here.\n{url}"
        )
    # A zero-exit gh with no captured URL: the Issue WAS created — never say "nothing submitted" (that would
    # push the operator to file a duplicate). Report it filed and point them to find the link.
    return (
        f"I've filed the issue on {upstream_repo} — it was created, but I couldn't capture the link back from "
        "GitHub. You'll find it in that project's issues list; nothing failed, so please don't file it again."
    )


def _degraded_narration(upstream_repo: str) -> str:
    return (
        f"I couldn't reach {upstream_repo} to file the issue — it was not created, so nothing was submitted and "
        "nothing is lost. I've drafted the exact title and body so it can be filed once the project is "
        "reachable, or you can open it yourself."
    )


# ---- telemetry: the stalled-contribution trace (into the operator's OWN repo) -----------------

_UNSET = object()  # sentinel: distinguishes "resolve the real boundary" from "offline (None)"


def _github():
    """The engine-Issue boundary, repo/token from boot's single source (lazy — kept off import for the common
    offline path). None when repo/token are unavailable → tracking degrades to surfaced-not-tracked (the
    submit.py precedent). Always the operator's OWN repo, never the contribution target."""
    try:
        from boot import repo_slug, gh_token  # lazy: only reached when actually tracing a stall
        repo, token = repo_slug(), gh_token()
    except Exception:  # noqa: BLE001 — any failure obtaining GitHub context → no durable tracking
        return None
    if not repo or not token:
        return None
    return telemetry.GitHubIssues(repo, token)


def _promote(record: dict, now: str, *, github=_UNSET):
    """Best-effort durable tracking of the stalled contribution. Returns the Issue number on success, or False
    when GitHub is unavailable (the stall was already surfaced in-session). `github` is injectable for the
    demo/tests (None = OFFLINE; omitting it resolves the real boundary)."""
    gh = _github() if github is _UNSET else github
    if gh is None:
        return False
    return telemetry.promote_finding(gh, record, now)


def _stalled_record(upstream_repo: str, title: str, err: str, now: str) -> dict:
    """A finding-record.v1 for a contribution Issue that was ready but could not be filed. Persistent-but-benign
    — a recurring local hiccup (an unreachable target), not a trust weakening; operator-facing message (plain
    language, no backstage vocabulary), matching submit.py's stalled-submission record."""
    return {
        "source_id": "external-contribution/stalled-issue",
        "severity": telemetry.PERSISTENT_BENIGN,
        "message": (f"An issue for {upstream_repo} (“{title}”) is ready but couldn't be filed "
                    f"({err or 'the project was unreachable'}). It's drafted for you to file."),
        "location": None,
        "first_seen": now,
        "last_seen": now,
    }


def _draft_text(upstream_repo: str, title: str, body: str) -> str:
    """The plain draft of a stalled contribution Issue — the exact title and body for the operator to file
    themselves. Deliberately NOT the engine-domain body contract: this is the contribution itself, authored for
    the target repo, not an item the Engine opened about itself."""
    return (
        f"An issue ready to file on {upstream_repo}:\n\n"
        f"Title:\n  {title}\n\n"
        f"Body:\n{body}\n"
    )


# ---- the contribution orchestration -----------------------------------------------------------

def _run_gh(args: list):
    """Run a `gh` command. Returns (returncode, stdout, stderr). Never raises — a missing/failed `gh` degrades
    to a non-zero return so the caller takes the degradation path. Used for BOTH the remote template fetch and
    the filing; the real `gh issue create` is the one boundary that acts on the network (tests and the
    demo inject a fake `gh_run`)."""
    import subprocess
    try:
        out = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60, check=False)
        return out.returncode, out.stdout.strip(), out.stderr.strip()
    except Exception as exc:  # noqa: BLE001 — missing gh / OS error / timeout → degrade
        return 1, "", str(exc)


def contribute_issue(*, upstream_repo: str, kind: str | None, summary: str,
                     gh_run=None, github=_UNSET,
                     confirm: bool = False, now: str | None = None) -> dict:
    """Prepare (and, on an explicit affirmative decision, file) an Issue contributed into `upstream_repo`,
    following that repo's own issue-title convention (read remotely from the target).

    Returns a result dict whose `status` is one of:
      - `"needs-summary"`     — the summary was blank; NOTHING is read or filed. Carries the plain-language
        `narration` asking for a one-line description.
      - `"kind-choice-needed"`— the target defines templates but `kind` did not resolve to one; NOTHING is
        filed. Carries `kinds` (the target's own kinds) and the plain-language `narration` — the engine never
        guesses which kind a contribution is.
      - `"prepared"`      — the title (prefixed per the target, or plain when the target/kind has no prefix) and
        the body are assembled, but no affirmative decision yet; the Issue is NOT filed. Carries the assembled
        `issue` (repo/title/body/kind/followed_convention/title_prefixed) and the prepared `narration`.
      - `"filed"`         — `confirm=True` and `gh issue create` returned zero (the Issue was created). Carries
        its `url` (or None if the link couldn't be captured — still filed) and the `narration`.
      - `"degraded-draft"`— `confirm=True` but `gh issue create` returned non-zero (NOT created); the prepared
        title+body are returned as a `draft` for the operator to file. Carries `issue`, `draft`, `promoted`
        (the stalled-contribution trace into the operator's OWN repo), and the `narration`.

    Every network boundary is injectable for offline proof: `gh_run` (the gh transport, used for BOTH the
    template fetch and the filing) and `github` (telemetry boundary). The real `gh issue create` is reached only
    when `confirm=True`.
    """
    now = now or telemetry.utc_now()
    gh = gh_run or _run_gh

    # 0. A contribution needs something to say. Refuse a blank summary before reading or filing anything, so a
    #    contentless, prefix-only Issue (title just "Bug: ", an empty body) can never be created.
    if not summary or not summary.strip():
        return {"status": "needs-summary", "narration": _needs_summary_narration(upstream_repo)}

    # 1. Follow the target's convention: read its issue templates REMOTELY and resolve the requested kind. When
    #    the target has templates but the kind doesn't name one, surface the choices — never guess (nothing filed).
    templates = detect_upstream_issue_templates(upstream_repo, gh_run=gh)
    title, matched = build_issue_title(kind=kind, summary=summary, templates=templates)
    if templates and matched is None:
        return {"status": "kind-choice-needed", "kinds": templates,
                "narration": _kind_choice_narration(upstream_repo, templates)}

    # 2. Assemble the body to the matched template (host's form), else a plain summary. `title_prefixed` records
    #    whether a real (non-empty) prefix was actually applied, so the narration never claims a heading the
    #    matched template didn't carry (a `title:`-less template is valid and common on GitHub).
    prefixed = bool(matched and str(matched.get("title_prefix", "")).strip())
    body = build_issue_body(summary=summary, template_text=(matched or {}).get("body_text"))
    issue = {"repo": upstream_repo, "title": title, "body": body,
             "kind": (matched or {}).get("key"),
             "followed_convention": matched is not None,  # the body followed the target's template
             "title_prefixed": prefixed}                  # a non-empty title prefix was actually applied

    # 3. The human gate: without an affirmative decision, PREPARE only — never file the Issue.
    if not confirm:
        return {"status": "prepared", "issue": issue,
                "narration": _prepared_narration(upstream_repo, title, prefixed)}

    # 4. File the Issue (the one boundary that acts on the network). A ZERO exit means it was created — report filed
    #    even if the URL couldn't be captured (never "nothing submitted", which would risk a duplicate). Only a
    #    NON-zero exit means it was not created → degrade to a drafted issue.
    try:
        rc, out, err = gh(["issue", "create", "--repo", upstream_repo, "--title", title, "--body", body])
    except Exception as exc:  # noqa: BLE001 — a misbehaving transport degrades like an unreachable target
        rc, out, err = 1, "", str(exc)
    if rc == 0:
        return {"status": "filed", "url": (out or None), "issue": issue,
                "narration": _filed_narration(upstream_repo, out or None)}
    promoted = _promote(_stalled_record(upstream_repo, title, err, now), now, github=github)
    return {"status": "degraded-draft", "issue": issue,
            "draft": _draft_text(upstream_repo, title, body),
            "promoted": promoted, "error": err,
            "narration": _degraded_narration(upstream_repo)}


# ---- falsifiable, offline demo (drives the REAL contribute_issue; prints the real narration) ---

def _demo_gh(*, has_templates: bool, templates: dict | None = None, create=(0, "https://github.com/upstream/project/issues/7", "")):
    """A fake `gh` for the demo/tests: serves `gh api` contents (directory listing + raw file) for a target's
    `.github/ISSUE_TEMPLATE`, and `gh issue create`. Runs the REAL detection/flow with no network. `templates`
    maps filename -> file text; `has_templates` False makes every contents call a 404. `create` is the canned
    `gh issue create` result. Records the last create call for assertions."""
    templates = templates if templates is not None else {}
    recorded: dict = {}

    def gh(args):
        if args and args[0] == "api":
            path = args[-1]
            after = path.split("/contents/", 1)[1] if "/contents/" in path else ""
            if after == ".github/ISSUE_TEMPLATE":
                if not has_templates:
                    return 1, '{"message":"Not Found"}', "gh: Not Found (HTTP 404)"
                listing = [{"name": n, "type": "file"} for n in sorted(templates)]
                return 0, json.dumps(listing), ""
            if after.startswith(".github/ISSUE_TEMPLATE/"):
                fname = after.rsplit("/", 1)[-1]
                if has_templates and fname in templates:
                    return 0, templates[fname], ""
                return 1, '{"message":"Not Found"}', "gh: Not Found (HTTP 404)"
            return 1, '{"message":"Not Found"}', "gh: Not Found (HTTP 404)"  # docs/ fallback etc.
        if args[:2] == ["issue", "create"]:
            recorded["create"] = args
            return create
        return 1, "", "unexpected gh call"

    gh.recorded = recorded  # type: ignore[attr-defined]
    return gh


_DEMO_TEMPLATES = {
    "bug.md": "---\nname: Bug report\nabout: Something isn't working.\ntitle: 'Bug: '\n---\n\n**What happened**\n",
    "feature.md": "---\nname: Feature request\nabout: Ask for something new.\ntitle: 'Feature: '\n---\n\n"
                  "**The need**\n",
    # A valid template with NO `title:` — GitHub allows this; the tool must file a plain title and say so.
    "question.md": "---\nname: Question\nabout: Ask a question.\n---\n\n**Your question**\n",
}


def demo() -> int:
    """Prove the issue-contribution flow over injected boundaries — and PRINT the actual operator-facing
    narration so a reviewer reads the words, not just PASS/FALSE. Cases: a blank summary is refused; an unknown
    kind against a target WITH templates surfaces the choices and files nothing; a resolved kind carries that
    kind's own prefix, read from the target REMOTELY; a matched template with no prefix files a plain title and
    says so; a clean prepare does NOT file; a decision files following the target's convention; a zero-exit gh
    with no URL is still reported filed (never a duplicate); a target with NO templates files a plain title; and
    an unreachable target degrades to a drafted issue, whose draft is printed. RETURNS NON-ZERO if any invariant
    breaks. Fully offline: the `gh` transport is faked, so no gh/network runs."""
    with_gh = _demo_gh(has_templates=True, templates=_DEMO_TEMPLATES)
    failures = []
    print("(This is a dry run against a pretend project — no real repository is touched and nothing is "
          "filed. It shows what the engine would say and do at each point.)\n")

    # Case A — a blank summary is refused before anything is read or filed.
    ra = contribute_issue(upstream_repo="upstream/project", kind="bug", summary="   ",
                          gh_run=with_gh, github=None, confirm=True)
    if ra["status"] != "needs-summary" or "create" in with_gh.recorded:
        failures.append(f"blank-summary case: expected needs-summary and nothing filed, got {ra['status']}")

    # Case B — an unknown kind against a target WITH templates: surface the choices, file NOTHING (even with
    #          confirm=True — the engine never guesses which kind a contribution is).
    rb = contribute_issue(upstream_repo="upstream/project", kind="banana",
                          summary="the login page 500s", gh_run=with_gh, github=None, confirm=True)
    print("--- an unknown kind: the engine asks which, and files nothing ---")
    print(rb["narration"], "\n")
    if rb["status"] != "kind-choice-needed" or "create" in with_gh.recorded:
        failures.append(f"kind-choice case: expected kind-choice-needed and nothing filed, got {rb['status']}")
    if {k["key"] for k in rb.get("kinds", [])} != {"bug", "feature", "question"}:
        failures.append("kind-choice case: the target's own kinds were not read remotely / surfaced")

    # Case C — a resolved kind carries THAT kind's own prefix, read from the target remotely.
    rc_ = contribute_issue(upstream_repo="upstream/project", kind="bug",
                           summary="the login page 500s", gh_run=with_gh, github=None, confirm=False)
    print("--- a known kind, prepared: it carries the target's own prefix ---")
    print(rc_["narration"], "\n")
    if rc_["status"] != "prepared" or rc_["issue"]["title"] != "Bug: the login page 500s" \
            or not rc_["issue"]["title_prefixed"]:
        failures.append(f"prepared case: title didn't follow the target's Bug prefix, got {rc_['issue']}")
    if "**What happened**" not in rc_["issue"]["body"]:
        failures.append("prepared case: the body did not follow the target's template")

    # Case D — a matched template with NO `title:` prefix: plain title, and the narration says so (no false
    #          "carries the project's heading").
    rd = contribute_issue(upstream_repo="upstream/project", kind="question",
                          summary="how do I configure X", gh_run=with_gh, github=None, confirm=False)
    print("--- a kind whose template has no heading: a plain title, said plainly ---")
    print(rd["narration"], "\n")
    if rd["issue"]["title"] != "how do I configure X" or rd["issue"]["title_prefixed"]:
        failures.append(f"no-prefix case: expected a plain title not marked prefixed, got {rd['issue']}")
    if "carries that project's own issue heading" in rd["narration"]:
        failures.append("no-prefix case: narration claimed a heading that wasn't applied")

    # Case E — a decision FILES, following the target's convention.
    with_gh.recorded.clear()
    re_ = contribute_issue(upstream_repo="upstream/project", kind="feature",
                           summary="add dark mode", gh_run=with_gh, github=None, confirm=True)
    print("--- a known kind, authorized: filed with the target's prefix ---")
    print(re_["narration"], "\n")
    if re_["status"] != "filed" or re_.get("url") != "https://github.com/upstream/project/issues/7":
        failures.append(f"filed case: expected filed with a url, got {re_['status']} / {re_.get('url')}")
    created = with_gh.recorded.get("create", [])
    if created[:2] != ["issue", "create"] or "Feature: add dark mode" not in created:
        failures.append(f"filed case: gh issue create not invoked with the prefixed title, got {created}")

    # Case F — a zero-exit gh with NO url captured: still reported FILED (never "nothing submitted"), so the
    #          operator is never nudged to file a duplicate.
    gh_no_url = _demo_gh(has_templates=True, templates=_DEMO_TEMPLATES, create=(0, "", ""))
    rf = contribute_issue(upstream_repo="upstream/project", kind="bug", summary="x",
                          gh_run=gh_no_url, github=None, confirm=True)
    if rf["status"] != "filed" or "nothing was submitted" in rf["narration"]:
        failures.append(f"filed-no-url case: a created issue was narrated as not submitted, got {rf['status']}")

    # Case G — a target with NO issue templates: files a plain (unprefixed) title.
    plain_gh = _demo_gh(has_templates=False)
    rg = contribute_issue(upstream_repo="plain/project", kind="bug", summary="a plain report",
                          gh_run=plain_gh, github=None, confirm=False)
    if rg["status"] != "prepared" or rg["issue"]["title"] != "a plain report" \
            or rg["issue"]["title_prefixed"]:
        failures.append(f"no-templates case: expected a plain unprefixed prepared title, got {rg['issue']}")

    # Case H — a decision but the target is unreachable (non-zero create): degrades to a drafted issue, printed.
    fail_gh = _demo_gh(has_templates=True, templates=_DEMO_TEMPLATES,
                       create=(1, "", "could not resolve host github.com"))
    rh = contribute_issue(upstream_repo="upstream/project", kind="bug", summary="the login page 500s",
                          gh_run=fail_gh, github=None, confirm=True)
    print("--- the target was unreachable: degraded to a drafted issue ---")
    print(rh["narration"])
    print(rh.get("draft", ""), "\n")
    if rh["status"] != "degraded-draft" or "Bug: the login page 500s" not in rh.get("draft", ""):
        failures.append(f"degrade case: expected degraded-draft carrying the prefixed title, got {rh['status']}")

    if failures:
        print("DEMO FAILED — the issue contribution broke an invariant:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("DEMO PASSED — a blank summary is refused; an unknown kind is surfaced for your choice (never guessed) "
          "and files nothing; a known kind carries the TARGET repo's own prefix, read remotely; a template with "
          "no heading files a plain title and says so; a clean contribution is only PREPARED until you authorize "
          "it; a created issue is reported filed even without a captured link (never a duplicate); a target with "
          "no templates gets a plain title; and an unreachable target degrades to a drafted issue, nothing lost.")
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return demo()
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
