"""Backend seam for provider-independent serialized cross-PR integration (StarshipSuperjam/engine-template#925).

Two backends sit behind ONE narrow interface. The interface is deliberately narrow — only what BOTH a
GitHub-native merge queue and the Engine-controlled serialized fallback can honestly satisfy: disclose
availability, admit one candidate into the ordered path, read who is admitted, release. It does NOT bake in
serialized-only assumptions, so StarshipSuperjam/engine-template#989 can fill the native backend without reshaping the seam.

  - `SerializedFallbackBackend` — BUILT here. Admission is a singleton `engine-integrating` label on the one
    admitted PR, acquired by a compare-and-swap that is HONESTLY advisory: GitHub's label API is not atomic
    and its list reads are eventually consistent, so the CAS reduces concurrent-admission collisions but is
    not a mutex. That is acceptable because integration safety does NOT rest on it — the coordinator never
    merges, `pr_reconcile.prepare` is serialized per branch by git's non-fast-forward push rejection, and the
    StarshipSuperjam/engine-template#915 freshness ruleset refuses a stale-green merge at the operator's click. Works on any repo/plan.
  - `NativeMergeQueueBackend` — a DISCLOSED STUB. `available()` returns False naming the real constraint (a
    `merge_group` trigger forces engine-guard onto the head-tainted merge commit, breaking its base-only
    trusted-base isolation — StarshipSuperjam/engine-template#989). Its methods document how each maps to GitHub's merge-queue API so
    StarshipSuperjam/engine-template#989 fills them against a validated target:
        admit    → add the PR to the branch's merge queue (the queue owns ordering + freshness re-check)
        admitted → read the queue entry currently being validated/merged
        release  → the queue dequeues on merge or failure; no engine action

`select_backend` tries native first and falls back to serialized, disclosing the fallback in plain words —
the same degrade-and-disclose posture as the `protection` check's plan-limitation path.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Callable, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import issue_label_client  # noqa: E402

INTEGRATING_LABEL = "engine-integrating"
READY_LABEL = "engine-integrate-ready"
PRIORITY_LABEL = "engine-integrate-priority"

# The labels this subsystem owns — the control plane provisions them (eADR-0021's provisioning law: producers
# apply, consumers read, provisioning creates), so no first-run gate blocks waiting on a hand-created label.
INTEGRATION_LABELS = [
    (READY_LABEL, "1d76db",
     "A reviewed pull request ready to enter the serialized integration path."),
    (PRIORITY_LABEL, "5319e7",
     "Promote this candidate ahead of FIFO order in the serialized integration path."),
    (INTEGRATING_LABEL, "0e8a16",
     "This pull request holds the single integration slot — one candidate integrates at a time."),
]


@dataclass(frozen=True)
class Admission:
    """The outcome of trying to admit one candidate into the ordered integration path."""

    pr: Optional[int]
    acquired: bool
    holder: Optional[int]      # the PR currently holding admission (may be another session's candidate)
    disclosure: str            # plain-language account of what happened / why


class NativeMergeQueueBackend:
    """The GitHub-native merge-queue backend — a DISCLOSED STUB until StarshipSuperjam/engine-template#989 resolves the engine-guard
    trusted-base isolation conflict. The method bodies document the native mapping so StarshipSuperjam/engine-template#989 fills them without
    reshaping the seam (see the module docstring)."""

    name = "native"

    def available(self, base_branch: str) -> tuple[bool, str]:
        # Operator-facing: plain language, no CI internals. The engineering reason (a merge_group trigger
        # would force engine-guard onto the head-tainted merge commit, breaking its base-only trusted-base
        # isolation) is recorded in the class docstring and StarshipSuperjam/engine-template#989.
        return (False,
                "GitHub's built-in merge queue isn't used yet — turning it on would weaken a safety check "
                "that guards your pull requests, so it's held until that's resolved (tracked as "
                "StarshipSuperjam/engine-template#989). The Engine integrates one candidate at a time instead; for you it works the same.")

    def admit(self, pr: int) -> Admission:      # pragma: no cover — stub until StarshipSuperjam/engine-template#989
        raise NotImplementedError("native merge-queue admit is StarshipSuperjam/engine-template#989: add the PR to the branch merge queue")

    def admitted(self) -> Optional[int]:        # pragma: no cover — stub until StarshipSuperjam/engine-template#989
        raise NotImplementedError("native merge-queue admitted is StarshipSuperjam/engine-template#989: read the queue's active entry")

    def release(self, pr: int) -> None:         # pragma: no cover — stub until StarshipSuperjam/engine-template#989
        raise NotImplementedError("native merge-queue release is StarshipSuperjam/engine-template#989: the queue dequeues on merge/failure")


class SerializedFallbackBackend:
    """The Engine-controlled serialized fallback — the one built here. Admission = a singleton
    `engine-integrating` label. `transport(method, path, body) -> (status, json|None)` is injectable so a
    demo/test fakes only the network and runs the real CAS logic."""

    name = "serialized"

    def __init__(self, repo: str, token: str, *, transport: Optional[Callable] = None,
                 user_agent: str = "engine-integration-queue"):
        self.repo = repo
        self._transport = transport
        self._labels = issue_label_client.IssueLabelClient(
            repo, token, user_agent=user_agent, transport=transport)

    def available(self, base_branch: str) -> tuple[bool, str]:
        return (True, "Engine-controlled serialized integration (one candidate at a time; works on any "
                      "repository and plan).")

    def _open_pulls(self) -> Optional[list]:
        """Open PRs, or None when the read FAILED (distinct from an empty list). A failed read must not read
        as 'no one holds admission' — the caller decides fail-open (advisory display) vs fail-closed (the CAS
        re-check)."""
        transport = self._transport or self._labels._http
        status, pulls = transport("GET", f"/repos/{self.repo}/pulls?state=open&per_page=100", None)
        if status >= 400 or not isinstance(pulls, list):
            return None
        return pulls

    def _holders(self) -> Optional[list[int]]:
        """Every open PR currently carrying the singleton admission label, or None if the read failed (list
        reads are eventually consistent, so even a successful read can lag — the CAS is advisory, never a
        mutex)."""
        pulls = self._open_pulls()
        if pulls is None:
            return None
        held = []
        for pr in pulls:
            names = [lab.get("name") for lab in pr.get("labels", []) if isinstance(lab, dict)]
            if INTEGRATING_LABEL in names:
                held.append(pr.get("number"))
        return [n for n in held if n is not None]

    def admitted(self) -> Optional[int]:
        holders = self._holders()
        return holders[0] if holders else None   # advisory display: an unknown (None) read shows no holder

    def admit(self, pr: int) -> Admission:
        """Compare-and-swap the singleton label: refuse if another PR already holds it; otherwise add it,
        re-read, and if MORE than one PR now holds it (a concurrent admission), drop ours and back off. This
        reduces collisions; it does not guarantee exclusion (see the module docstring). It never corrupts:
        the worst case is two candidates briefly admitted, both of which the per-branch prepare + the merge
        ruleset still serialize safely."""
        holder = self.admitted()
        if holder is not None and holder != pr:
            return Admission(pr, False, holder, f"PR #{holder} is currently integrating — not admitting yet.")
        self._labels.add_label(pr, INTEGRATING_LABEL)
        holders = self._holders()
        if holders is None:
            # the re-read FAILED — we cannot confirm we are the sole holder, so fail CLOSED: drop ours and
            # back off rather than proceed on an unknown (a failed read must never read as "slot free").
            self._labels.remove_label(pr, INTEGRATING_LABEL)
            return Admission(pr, False, None,
                             "Couldn't confirm the integration slot is free (a read failed) — backing off; try again.")
        if len([h for h in holders if h != pr]) >= 1:
            # a concurrent session admitted a DIFFERENT PR at the same time — both back off, drop ours.
            self._labels.remove_label(pr, INTEGRATING_LABEL)
            return Admission(pr, False, None, "Another candidate was admitted concurrently — backing off.")
        return Admission(pr, True, pr, f"PR #{pr} admitted for integration.")

    def release(self, pr: int) -> None:
        self._labels.remove_label(pr, INTEGRATING_LABEL)


def select_backend(repo: str, token: str, base_branch: str, *, tier: str,
                   transport: Optional[Callable] = None):
    """Return (backend, disclosure). Try the native queue first; today its `available()` is False, so this
    returns the serialized fallback and discloses why. When StarshipSuperjam/engine-template#989 lands, native.available() flips True
    where GitHub offers a queue AND the guard-isolation fix is present — only that predicate changes."""
    native = NativeMergeQueueBackend()
    ok, why = native.available(base_branch)
    if ok:                                       # pragma: no cover — until StarshipSuperjam/engine-template#989
        return native, why
    return SerializedFallbackBackend(repo, token, transport=transport), why
