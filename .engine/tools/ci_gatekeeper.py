#!/usr/bin/env python3
"""ci_gatekeeper — decide whether an engine-ci run owes the full inventory or may reuse an earlier proof.

WHY THIS EXISTS. The required `engine-ci` check re-ran the structural validator and the whole behavioural
self-test inventory on every pull-request event, including `edited`, `labeled` and `unlabeled` — events that
cannot change the code under test. Correcting a pull-request body, or applying an acknowledgement label, spent
the whole suite again for an answer already known. This module implements one narrow principle:

    Never re-run the full suite on a tree that has already passed it.

New or changed code still earns a full run every time. Only a REPEAT judgment of an unchanged tree is replaced
by verifying the receipt an earlier genuine full run left behind.

THE BINDING IS THE TREE, NOT A COMMIT. A pull-request run checks out `refs/pull/N/merge` — head merged into
base — so neither the head commit nor the base commit alone identifies what was tested. The merge COMMIT is
no good either: its sha varies with the committer timestamp, so the platform can mint a different sha for an
identical tree. What is stable is git's content-addressed TREE hash: `HEAD^{tree}` is identical if and only if
the checked-out content is identical. That is what a receipt attests and what reuse compares, so this module
depends on no platform field for its central claim. Head and base are carried too, but only as a cheap
pre-filter and as diagnostics.

TRUST COMES FROM PLATFORM METADATA, NEVER FROM THE RECEIPT'S OWN CLAIMS. Adding a new workflow file is only a
soft, non-blocking disclosure under the weakening guard, so a pull request could add a sibling workflow that
uploads an artifact under this receipt's exact name. The load-bearing defence is therefore the CANDIDATE FILTER:
a run qualifies only if the Actions API reports it as a run of `.github/workflows/engine-ci.yml` (the file PATH,
never the `name:` field, which any workflow may duplicate), with conclusion `success`, and with the head commit
this event is about. Only then is the receipt body read, and only from that run's own artifact list. No value
taken from a receipt is ever used to select the run that vouches for it.

ONLY A FULL RUN UPLOADS. A reuse run uploads nothing, so artifact presence is the structural marker that
separates a genuine full run from a reuse run of the same workflow — which run metadata alone cannot do, since
it reports the event but not the action. That is also why the candidate search must ENUMERATE every matching
successful run rather than taking the newest: a reuse run is itself a successful run of this workflow at this
head, so a select-newest-then-verify implementation would pick the reuse run, find no artifact, and fall back to
a full run — silently paying full cost for every metadata event after the first.

EVERY FAILURE RESOLVES TO MORE WORK. An authorization error, an API failure, a malformed or expired artifact, a
receipt that does not match: all resolve to `full`. There is no path on which a failure yields `reuse`, and no
path on which this module reports success. Because that degradation is silent by construction, every full
decision carries a machine-readable REASON, and the workflow surfaces it when a metadata event had to run full —
the case where reuse was expected and did not happen — so a permanently broken receipt path cannot masquerade as
normal operation.

GUARDRAIL-CLASS. This module decides whether the frozen `engine-ci` context may report success without running
the inventory in that run. Weakening its decision or its verification has no on-disk correlate a reviewer would
notice, and the same pull request can edit both this file and its tests — the argument that already places
`mechanic_build.py` and `ack_status.py` in the guard's hard tier. It is a member of `_FLOOR_ENFORCEMENT_HOOKS`
and of `_HARD_EXACT`: modifying it requires a deliberate acknowledgement. Any helper module this grows joins
both sets in the same change.

stdlib-only. The GitHub reads go through `github_client`, which owns the authenticated-request shape and the
off-host guard; the helpers it gained for this module are dumb transport (list runs, list a run's artifacts,
download one by id) and carry no filtering, no path matching and no conclusion checks — every trust predicate
lives here.
"""
from __future__ import annotations

import datetime
import hashlib
import io
import json
import moment
import os
import subprocess
import sys
import zipfile

# The frozen identities this module is bound to. The workflow PATH (never its display name) is what makes a
# candidate run trustworthy; the artifact name is what a full run uploads and a reuse run does not.
WORKFLOW_PATH = ".github/workflows/engine-ci.yml"
CHECK_CONTEXT = "engine-ci"
RECEIPT_ARTIFACT_NAME = "engine-ci-receipt"
RECEIPT_FILENAME = "receipt.json"
RECEIPT_SCHEMA = "engine-ci-receipt/v1"

# A receipt older than this is refused. Retention on the artifact is deliberately longer, so expiry is always
# this module's decision rather than a mysteriously missing artifact.
MAX_RECEIPT_AGE_DAYS = 14

# How many pages of candidate runs to walk before giving up (and resolving to full).
_MAX_CANDIDATE_PAGES = 3
_RUNS_PER_PAGE = 100

MODE_FULL = "full"
MODE_REUSE = "reuse"

# The environment names the workflow branches on. MODE_ENV carries the decision; RAN_ENV is written by an arm
# when it FINISHES, and the two are deliberately different: a marker set by the decision step would prove only
# that the decision ran, which is exactly the thing the terminal assertion must not accept as proof that an
# arm ran. The arms condition on MODE_ENV; the terminal step reads RAN_ENV. The reason a full run was chosen is
# disclosed in the job summary and stdout, not published to the environment — no step conditions on it.
MODE_ENV = "ENGINE_CI_MODE"
RAN_ENV = "ENGINE_CI_RAN"

# The actions that cannot change the tree under test. Everything else — including an action this module does
# not recognise — is a code event and earns a full run.
METADATA_ACTIONS = frozenset({"edited", "labeled", "unlabeled"})
CODE_ACTIONS = frozenset({"opened", "synchronize", "reopened"})

# Machine-readable reasons a run resolved to `full`. The first three are ordinary and expected; the rest mean
# reuse was possible in principle and did not happen, which is what the workflow surfaces on a metadata event.
REASON_NOT_PULL_REQUEST = "not-a-pull-request"
REASON_CODE_EVENT = "code-event"
REASON_UNRECOGNISED_ACTION = "unrecognised-action"
REASON_NO_RECEIPT = "no-receipt-for-this-tree"
REASON_DISCOVERY_FAILED = "receipt-discovery-failed"
REASON_REFUSED = "receipt-refused"
# The candidate list for this head exceeded the page budget and no valid receipt was found among the runs we
# read. Distinct from REASON_NO_RECEIPT so a head whose run count has outgrown the budget (reuse silently
# stops paying off) is DISTINGUISHABLE from a tree that simply never had a full run — the module's
# distinct-reason principle applied to the one give-up path that was otherwise indistinguishable.
REASON_CANDIDATE_LIST_TRUNCATED = "candidate-list-truncated"

_USER_AGENT = "engine-ci-gatekeeper"


class GatekeeperError(Exception):
    """A refusal this module cannot degrade past — raised only where a caller must stop, never for a
    discovery failure (which resolves to a full run instead)."""


# --------------------------------------------------------------------------------------------------
# The tree identity
# --------------------------------------------------------------------------------------------------

def tree_sha(root: str | None = None) -> str:
    """The content-addressed git tree hash of the current checkout — the identity a receipt attests.

    `HEAD^{tree}` reads the tree sha out of the commit object's header, so it works at the default shallow
    fetch depth (the parent objects are absent, but the commit's own tree entry is not). Two checkouts with
    identical content have identical tree hashes no matter how their commits were minted, which is precisely
    the property the merge-commit sha lacks."""
    return _git(["rev-parse", "HEAD^{tree}"], root)


def head_and_base(root: str | None = None):
    """`(head, base)` for a pull-request checkout, read from the merge commit's own parent list.

    `refs/pull/N/merge` records parent 1 = the base branch tip and parent 2 = the pull-request head. Reading
    them from the commit header (`log -1 --format=%P`) needs no parent objects, so this too survives a shallow
    fetch. Returns `(None, None)` on a checkout that is not a merge — a push to the default branch, where the
    question does not arise."""
    parents = _git(["log", "-1", "--format=%P"], root).split()
    if len(parents) != 2:
        return None, None
    return parents[1], parents[0]


def _git(args, root: str | None = None) -> str:
    proc = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    if proc.returncode != 0:
        raise GatekeeperError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


# --------------------------------------------------------------------------------------------------
# The inventory the receipt attests
# --------------------------------------------------------------------------------------------------

def inventory_digest(root: str | None = None):
    """`(count, digest)` over the self-test modules the workflow's discovery would run.

    Derived through the assurance catalogue's OWN discovery so the receipt names exactly the inventory the
    published catalogue warrants, rather than a second, quietly diverging enumeration. The digest is a sha256
    over the sorted relative module paths, so adding or removing a test module changes it and a receipt whose
    inventory no longer matches is refused."""
    import ci_assurance  # lazy: keeps this module's import light and its dependency explicit
    import validate

    base = root or validate.ROOT
    _, steps = ci_assurance.workflow_facts(ci_assurance.load_workflow(base))
    modules = ci_assurance.discover_test_modules(base, steps)
    names = sorted(m["path"] for m in modules)
    joined = "\n".join(names)
    return len(names), "sha256:" + hashlib.sha256(joined.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------------------------------
# The decision
# --------------------------------------------------------------------------------------------------

def decide(event, *, repo, token, root=None, transport=None):
    """`(mode, reason, detail)` for one workflow run.

    `event` is the parsed webhook payload plus its event name, as `load_event` returns it. The rules, in order:

      - anything that is not a pull-request event (notably a push to the default branch) -> full. That run is
        the badge's witness, the default-branch telemetry signal, and the integration queue's green check, and
        it has no metadata to re-check;
      - a code action (opened / synchronize / reopened) -> full, regardless of any receipt, so new or changed
        code is never merged on reused evidence and a broken test still surfaces on the push that broke it;
      - an action this module does not recognise -> full, because the safe default for an unknown input to a
        merge gate is more work;
      - a metadata action -> reuse if and only if a receipt for THIS tree is found and verified; otherwise full,
        carrying the reason it could not.

    `detail` is the verified receipt and its source run on the reuse path (what the run must disclose), or the
    refusal detail on the full path. Never raises for a discovery failure: that resolves to full."""
    name = event.get("event_name") or event.get("name")
    if name != "pull_request":
        return MODE_FULL, REASON_NOT_PULL_REQUEST, None

    action = (event.get("payload") or {}).get("action")
    if action in CODE_ACTIONS:
        return MODE_FULL, REASON_CODE_EVENT, None
    if action not in METADATA_ACTIONS:
        return MODE_FULL, REASON_UNRECOGNISED_ACTION, {"action": action}

    pull = (event.get("payload") or {}).get("pull_request") or {}
    number = (event.get("payload") or {}).get("number") or pull.get("number")
    head_sha = (pull.get("head") or {}).get("sha")
    if not number or not head_sha:
        return MODE_FULL, REASON_DISCOVERY_FAILED, {"error": "the event payload carries no pull request head"}

    try:
        expected_tree = tree_sha(root)
    except GatekeeperError as exc:
        return MODE_FULL, REASON_DISCOVERY_FAILED, {"error": str(exc)}

    try:
        found, detail = find_reusable_receipt(
            repo=repo, token=token, pr_number=int(number), head_sha=head_sha,
            expected_tree=expected_tree, root=root, transport=transport)
    except Exception as exc:                      # noqa: BLE001 - any discovery failure means MORE work, never less
        return MODE_FULL, REASON_DISCOVERY_FAILED, {"error": f"{type(exc).__name__}: {exc}"}

    if found:
        return MODE_REUSE, None, detail
    return MODE_FULL, (detail or {}).get("reason") or REASON_NO_RECEIPT, detail


def find_reusable_receipt(*, repo, token, pr_number, head_sha, expected_tree, root=None, transport=None):
    """`(found, detail)` — walk every successful engine-ci run for this head and return the first that yields a
    receipt attesting `expected_tree`.

    ENUMERATION IS LOAD-BEARING, not an optimisation. A reuse run is itself a successful run of this workflow at
    this head, so it satisfies the candidate filter; taking only the newest match would pick it, find no
    artifact, and fall back to a full run — quietly destroying the saving for every metadata event after the
    first. Candidates are walked newest-first (the genuine full run is usually the oldest match) and the first
    valid receipt wins, deterministically."""
    transport = transport or _default_transport(token)
    refusals = []
    progress = {}
    for run in _candidate_runs(repo=repo, head_sha=head_sha, transport=transport, progress=progress):
        ok, why, receipt = _receipt_from_run(
            repo=repo, run=run, pr_number=pr_number, head_sha=head_sha,
            expected_tree=expected_tree, root=root, token=token, transport=transport)
        if ok:
            return True, {"run_id": run["id"], "run_url": run.get("html_url"),
                          "run_attempt": run.get("run_attempt"), "receipt": receipt}
        refusals.append({"run_id": run.get("id"), "why": why})
    # Truncation is reported EVEN IF some candidates were refused: a valid receipt might sit in the pages we
    # did not read, so a truncated give-up must not masquerade as an ordinary no-receipt/refused result.
    if progress.get("truncated"):
        reason = REASON_CANDIDATE_LIST_TRUNCATED
    elif refusals:
        reason = REASON_REFUSED
    else:
        reason = REASON_NO_RECEIPT
    return False, {"reason": reason, "refusals": refusals, "truncated": progress.get("truncated", False)}


def _candidate_runs(*, repo, head_sha, transport, progress=None):
    """Successful runs of THIS workflow for THIS head commit, newest first, from platform-reported metadata only.

    Selection never consults a value taken from a receipt: the workflow is identified by its file `path` (a
    display `name` can be duplicated by any workflow a pull request adds), the conclusion must be `success`, and
    the head commit the platform reports for the run must equal the one this event is about.

    The runs listing is head-scoped across ALL workflows, so a long-churned head can exceed the page budget.
    If every page up to the budget comes back full — meaning more runs exist beyond what we read — `progress`
    (when supplied) is marked `truncated`, so the caller can report a distinct give-up reason rather than a
    silent no-receipt."""
    for page in range(1, _MAX_CANDIDATE_PAGES + 1):
        path = (f"/repos/{repo}/actions/runs?head_sha={head_sha}&status=completed"
                f"&per_page={_RUNS_PER_PAGE}&page={page}")
        status, body = transport("GET", path, None)
        if status >= 400 or not isinstance(body, dict):
            raise GatekeeperError(f"listing workflow runs failed with status {status}")
        runs = body.get("workflow_runs") or []
        for run in runs:
            if run.get("path") != WORKFLOW_PATH:
                continue
            if run.get("conclusion") != "success":
                continue
            if run.get("head_sha") != head_sha:
                continue
            yield run
        if len(runs) < _RUNS_PER_PAGE:
            return
    # Fell through the whole page budget without a short (final) page: there may be more runs we did not read.
    if progress is not None:
        progress["truncated"] = True


def _receipt_from_run(*, repo, run, pr_number, head_sha, expected_tree, root, token, transport):
    """`(ok, why, receipt)` for one candidate run: find its receipt artifact, download it, and verify it."""
    status, body = transport("GET", f"/repos/{repo}/actions/runs/{run['id']}/artifacts?per_page=100", None)
    if status >= 400 or not isinstance(body, dict):
        return False, "artifact-listing-failed", None
    artifact = None
    for item in body.get("artifacts") or []:
        if item.get("name") == RECEIPT_ARTIFACT_NAME and not item.get("expired"):
            artifact = item
            break
    if artifact is None:
        # Not a defect: this is exactly how a REUSE run is recognised — it uploads nothing.
        return False, "no-receipt-artifact", None

    try:
        raw = download_artifact(repo=repo, artifact_id=artifact["id"], token=token)
        receipt = json.loads(_extract_receipt(raw))
    except Exception as exc:                       # noqa: BLE001 - a bad artifact is a refusal, never a pass
        return False, f"artifact-unreadable: {type(exc).__name__}", None

    ok, why = verify_receipt(receipt, repo=repo, pr_number=pr_number, head_sha=head_sha,
                             expected_tree=expected_tree, run=run, root=root)
    return ok, why, (receipt if ok else None)


def verify_receipt(receipt, *, repo, pr_number, head_sha, expected_tree, run, root=None, now=None):
    """`(ok, why)` — every field a receipt must satisfy to authorize reuse. Fails closed on anything unexpected.

    The tree hash is the substantive check: it is what makes "the same code, already judged" literally true.
    The rest bind the receipt to this repository, this pull request, this head, and the run it was found on, so
    a receipt copied between runs or pull requests is refused. `run` is the platform's own record of the
    producing run — the comparison for head and run identity is receipt-versus-platform, never
    receipt-versus-receipt."""
    if not isinstance(receipt, dict):
        return False, "receipt-not-an-object"
    if receipt.get("schema") != RECEIPT_SCHEMA:
        return False, "wrong-schema"
    if receipt.get("mode") != MODE_FULL:
        return False, "not-a-full-run-receipt"
    if receipt.get("repository") != repo:
        return False, "wrong-repository"
    if receipt.get("pr_number") != pr_number:
        return False, "wrong-pull-request"
    if receipt.get("head_sha") != head_sha:
        return False, "wrong-head-commit"
    if receipt.get("workflow_path") != WORKFLOW_PATH:
        return False, "wrong-workflow-path"
    if receipt.get("check_context") != CHECK_CONTEXT:
        return False, "wrong-check-context"
    if receipt.get("run_id") != run.get("id"):
        return False, "receipt-does-not-claim-the-run-it-was-found-on"
    if receipt.get("tree_sha") != expected_tree:
        return False, "different-tree"
    if receipt.get("result") != "success":
        return False, "receipt-does-not-record-success"

    age_reason = _age_ok(receipt.get("completed_at"), now)
    if age_reason:
        return False, age_reason

    # Re-derive the inventory rather than believing the receipt's own count: a receipt that attests a smaller
    # inventory than this checkout actually has is not evidence for this tree.
    try:
        count, digest = inventory_digest(root)
    except Exception:                              # noqa: BLE001 - cannot re-derive => cannot trust
        return False, "inventory-not-re-derivable"
    if receipt.get("test_module_digest") != digest or receipt.get("test_module_count") != count:
        return False, "inventory-mismatch"

    return True, None


def _age_ok(completed_at, now=None):
    if not isinstance(completed_at, str):
        return "no-completion-time"
    stamp = moment.parse_z(completed_at)
    if stamp is None:
        return "unparseable-completion-time"
    current = now or datetime.datetime.now(datetime.timezone.utc)
    if (current - stamp).days > MAX_RECEIPT_AGE_DAYS:
        return "receipt-too-old"
    return None


# A hard ceiling on the receipt member's UNCOMPRESSED size. A genuine receipt is a few hundred bytes; this
# refuses a zip-bomb entry whose declared size would balloon in memory before the JSON parse ever runs.
_MAX_RECEIPT_BYTES = 1 * 1024 * 1024


def _extract_receipt(zip_bytes: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        info = archive.getinfo(RECEIPT_FILENAME)
        if info.file_size > _MAX_RECEIPT_BYTES:
            raise ValueError("the receipt artifact's declared size exceeds the cap")
        return archive.read(RECEIPT_FILENAME).decode("utf-8")


def download_artifact(*, repo, artifact_id, token):
    """The receipt artifact's bytes, through the shared client's credential-stripping download seam."""
    import github_client  # lazy: keeps the offline unit tests free of the network module

    return github_client.download_redirected(
        f"/repos/{repo}/actions/artifacts/{artifact_id}/zip", token, user_agent=_USER_AGENT)


def _default_transport(token):
    import github_client  # lazy

    return lambda method, path, body=None: github_client.json_request(
        method, path, token, user_agent=_USER_AGENT, body=body)


# --------------------------------------------------------------------------------------------------
# The receipt a full run leaves behind
# --------------------------------------------------------------------------------------------------

def emit_receipt(event, *, repo, root=None, env=None, now=None):
    """The receipt a genuine full run uploads, as a dict.

    Emitted only after the validator and the inventory have both passed — the workflow orders the step that
    way, so the attestation is true by construction rather than by assertion. A reuse run never calls this."""
    environ = env if env is not None else os.environ
    pull = (event.get("payload") or {}).get("pull_request") or {}
    number = (event.get("payload") or {}).get("number") or pull.get("number")
    head, base = head_and_base(root)
    count, digest = inventory_digest(root)
    stamp = moment.to_z(now) if now is not None else moment.utc_now()
    return {
        "schema": RECEIPT_SCHEMA,
        "mode": MODE_FULL,
        "result": "success",
        "repository": repo,
        "pr_number": int(number) if number else None,
        "head_sha": head or (pull.get("head") or {}).get("sha"),
        "base_sha": base,
        "tree_sha": tree_sha(root),
        "workflow_path": WORKFLOW_PATH,
        "check_context": CHECK_CONTEXT,
        "run_id": _int_or_none(environ.get("GITHUB_RUN_ID")),
        "run_attempt": _int_or_none(environ.get("GITHUB_RUN_ATTEMPT")),
        "test_module_count": count,
        "test_module_digest": digest,
        "completed_at": stamp,
    }


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------------------------------
# What the run tells a person
# --------------------------------------------------------------------------------------------------

def reuse_disclosure(detail) -> str:
    """The first line of a reuse run's job summary, in plain words.

    A reuse run's green looks identical to a full run's green in the pull-request checks list. This line is the
    run's own account of the difference: it names the run whose proof was accepted, so a merge allowed by reuse
    stays reconstructable while the run's logs live. Where it surfaces, honestly: it is written to the run's
    Summary tab (one click past the check's Details link) AND printed to the decide step's log; the run's step
    list separately shows the validator and self-test steps as skipped. It is NOT the operator's primary
    disclosure — that is the pull-request body's standing statement, which needs no click at all; this line is
    the per-run detail for whoever opens the run."""
    receipt = (detail or {}).get("receipt") or {}
    return (
        f"Reused the proof from run {detail.get('run_id')} ({detail.get('run_url')}) for this exact tree "
        f"({receipt.get('tree_sha')}); the full self-test inventory was NOT re-run here. "
        f"That run checked {receipt.get('test_module_count')} self-test modules and passed."
    )


def full_disclosure(reason, detail) -> str:
    """The first line of a full run's job summary when a METADATA event had to run the inventory anyway.

    Reuse was expected here and did not happen, so the reason is stated where a person will see it. Without
    this, a permanently broken receipt path — a revoked scope, an API that stopped answering, uploads that
    silently stopped — would look exactly like normal operation forever."""
    extra = ""
    if isinstance(detail, dict) and detail.get("refusals"):
        first = detail["refusals"][0]
        extra = f" (nearest candidate run {first.get('run_id')}: {first.get('why')})"
    return (f"Ran the full self-test inventory on a metadata-only event because an earlier proof could not be "
            f"reused: {reason}{extra}.")


# --------------------------------------------------------------------------------------------------
# CLI — the three verbs the workflow calls
# --------------------------------------------------------------------------------------------------

def _load_event():
    """The workflow event, through the engine's existing event-loading seam."""
    import issue_event  # lazy

    payload = issue_event.load_event()
    return {"event_name": os.environ.get("GITHUB_EVENT_NAME"), "payload": payload or {}}


def main(argv):
    verb = argv[0] if argv else None
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")

    if verb == "decide":
        event = _load_event()
        mode, reason, detail = decide(event, repo=repo, token=token)
        _publish(MODE_ENV, mode)
        if mode == MODE_REUSE:
            line = reuse_disclosure(detail)
            print(line)
            # A reuse run's green is indistinguishable from a full run's green in the checks list, so this
            # summary line is the ONLY thing that tells a person the inventory did not run here. If it cannot
            # be written, the run has no way to disclose what it did — so it refuses rather than reporting a
            # green nobody can account for.
            if not _write_summary(line):
                print("engine-ci: refusing to reuse a proof this run cannot disclose "
                      "(no writable step summary).", file=sys.stderr)
                return 1
        elif reason not in (REASON_NOT_PULL_REQUEST, REASON_CODE_EVENT, REASON_UNRECOGNISED_ACTION):
            # Reuse was expected on a metadata-only event and did not happen. Say why, where a person will
            # see it: otherwise a permanently broken receipt path looks exactly like a normal full run. The
            # three ordinary reasons (not-a-PR, a code event, an unrecognised action) are plain full runs and
            # need no could-not-reuse note.
            line = full_disclosure(reason, detail)
            print(line)
            _write_summary(line)
        else:
            print(f"engine-ci: running the full inventory ({reason}).")
        return 0

    if verb == "emit-receipt":
        out = None
        for i, tok in enumerate(argv[1:]):
            if tok == "--out" and i + 2 <= len(argv[1:]):
                out = argv[1:][i + 1]
        if not out:
            print("emit-receipt needs --out <path>", file=sys.stderr)
            return 2
        receipt = emit_receipt(_load_event(), repo=repo)
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(receipt, fh, indent=2, sort_keys=True)
        print(f"receipt written for tree {receipt['tree_sha']} ({receipt['test_module_count']} modules)")
        return 0

    if verb == "assert-ran":
        # The terminal, UNCONDITIONED step. Two positively-conditioned arms are not exhaustive: if the mode
        # step ever wrote nothing, wrote an unexpected value, or its marker name drifted, every substantive
        # step would skip — and the platform treats a skipped step as successful, so the job would report
        # GREEN having proven nothing. This refuses that.
        marker = os.environ.get(RAN_ENV, "")
        if marker not in (MODE_FULL, MODE_REUSE):
            print(f"engine-ci: no arm recorded completion (marker={marker!r}); refusing to report success.",
                  file=sys.stderr)
            return 1
        print(f"engine-ci: {marker} arm completed.")
        return 0

    print(__doc__)
    return 0


def _publish(key, value):
    """Publish one verdict to the job ENVIRONMENT, where later steps' conditions can read it as `env.<key>`.

    Deliberately the environment rather than a step output: reading a step output requires the producing step
    to carry an `id:`, and the assurance generator refuses any workflow step key outside a fixed set — a set
    this change does not widen, because that refusal is itself a structural defence (the required gate cannot
    grow a shape the published catalogue is unable to describe). The environment file needs no step key at
    all, so the branch value travels with the generator untouched."""
    path = os.environ.get("GITHUB_ENV")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{key}={value}\n")


def _write_summary(line) -> bool:
    """Append one line to the run's job summary; `True` when it was actually written.

    Returns a verdict rather than swallowing the outcome, because on the reuse path a disclosure that cannot
    be written is a refusal, not a nicety — see the `decide` verb."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return False
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        return False
    return True


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
