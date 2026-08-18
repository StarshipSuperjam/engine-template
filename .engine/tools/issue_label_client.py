#!/usr/bin/env python3
"""The shared per-Issue label + minimal-mutation client — the ONE injectable-transport client the on:issues
backstops share for GitHub label reads/writes and the few whole-Issue mutations a backstop needs.

WHY THIS EXISTS. More than one on:issues CI backstop needs the same handful of per-Issue operations over the
same authenticated transport: the conformance net (`issue_conformance_ci`) ensures/adds/removes the
`needs-reauthoring` label, and the kind reconciler (`issue_kind_label`) checks/adds a GitHub-native label AND
repairs an Issue's title. Copying the transport into each would create near-identical HTTP clients that drift
on the next fix (a 404 tolerance, a timeout, a status rule). So the operations + the injectable
`transport(method, path, body) -> (status, json|None)` seam live once, here; each tool subclasses or holds one
and adds only what is truly its own (conformance keeps its comment operations; the reconciler keeps its title
mapping). The transport builds requests through the shared `github_client` (the telemetry / audit_digest
idiom), so this is the layer above that request builder, not a second request builder.

SCOPE (deliberately widened for StarshipSuperjam/engine-template#937). This started as a label-only client and
is now the per-Issue label PLUS the minimal whole-Issue reads/mutations a backstop genuinely needs — today a
live `get_issue` and a `edit_title` for the kind reconciler. It is still deliberately NOT
`telemetry.GitHubIssues`: that class is engine-issue-domain shaped (the `engine` label baked in, opens whole
Issues with a body) and pulls a heavy import chain onto the CI hot path; this exposes only the light operations
a per-Issue net needs, so the reconciler need not import that stack for a one-line title PATCH.

NEVER SWALLOWS A FAILURE. A GitHub API failure a backstop depends on raises `DegradedWriteError` — surfaced as
a red CI run (the net's own breakage is visible), never a silent pass. The one tolerated non-error is a 404 on
a read/remove: the label the caller asked about is simply not there (`label_exists` -> False; `remove_label`
-> a no-op, the desired state already holds).
"""
from __future__ import annotations

import os
import sys
import urllib.error
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import github_client  # noqa: E402  (the shared authenticated GitHub API client; request-build)


class DegradedWriteError(Exception):
    """Raised when a GitHub API call a backstop depends on fails. It is NEVER swallowed as success — a real
    API failure must surface as a red CI run (the net's own breakage is visible), never a silent pass."""


class IssueLabelClient:
    """The per-Issue label + minimal-mutation client. `transport(method, path, body) -> (status, json|None)` is
    injectable, so a demo or test fakes ONLY the network and runs the real logic. `user_agent` names the calling
    tool on the wire. Deliberately NOT telemetry.GitHubIssues — that class is engine-issue-domain shaped (label
    baked in, opens whole Issues) and heavy to import; this exposes only the light per-Issue operations a CI
    backstop needs: the label operations, plus a live `get_issue` read and a `edit_title` mutation."""

    def __init__(self, repo: str, token: str, *, user_agent: str, transport=None):
        self.repo = repo
        self.token = token
        self.user_agent = user_agent
        self._transport = transport or self._http

    def _http(self, method: str, path: str, body=None):
        # The shared JSON-transport mechanics (encode, build-with-off-host-guard, execute, HTTPError->status,
        # empty-body->None) live in github_client.json_request now; only this client's URLError POLICY — an
        # unreachable host is a WRITE failure — stays here.
        try:
            return github_client.json_request(method, path, self.token, user_agent=self.user_agent, body=body)
        except urllib.error.URLError as exc:             # network unreachable — a write failure
            raise DegradedWriteError(f"GitHub is unreachable: {exc}") from exc

    def label_exists(self, name: str) -> bool:
        """True iff the repo currently has a label of this name; False on a 404 (absent). Any other >= 400 is a
        real failure and raises. The name is URL-encoded — a label string is an operator-picked build-spec leaf
        that could carry a space or `/`, so it must never be interpolated raw into the URL."""
        status, _ = self._transport(
            "GET", f"/repos/{self.repo}/labels/{urllib.parse.quote(name, safe='')}", None)
        if status == 404:
            return False
        if status >= 400:
            raise DegradedWriteError(f"GitHub returned {status} checking the '{name}' label")
        return True

    def ensure_label(self, name: str, color: str, description: str) -> None:
        """Idempotently ensure a repo label exists (create it iff absent). Parametrised on
        name/color/description (telemetry's own ensure is hardcoded to the engine label)."""
        if not self.label_exists(name):
            self._transport("POST", f"/repos/{self.repo}/labels",
                            {"name": name, "color": color, "description": description})

    def add_label(self, number: int, name: str) -> None:
        status, _ = self._transport("POST", f"/repos/{self.repo}/issues/{number}/labels", {"labels": [name]})
        if status >= 400:
            raise DegradedWriteError(f"GitHub returned {status} adding '{name}' to issue #{number}")

    def remove_label(self, number: int, name: str) -> None:
        # 404 = the label was not on the Issue — a tolerated no-op (the state we wanted is already true).
        # The name is URL-encoded (the label is an operator-picked leaf that could carry a space/`/`).
        status, _ = self._transport(
            "DELETE", f"/repos/{self.repo}/issues/{number}/labels/{urllib.parse.quote(name, safe='')}", None)
        if status not in (200, 204, 404):
            raise DegradedWriteError(f"GitHub returned {status} removing '{name}' from issue #{number}")

    def get_issue(self, number: int) -> dict:
        """The Issue's CURRENT state (title, body, labels, …) read live. The kind reconciler uses this to
        recompute a title repair against the LATEST title immediately before writing, so a concurrent human edit
        made after the triggering event is not reverted (the event payload is a frozen snapshot). Raises on any
        read failure — never a silent stale read that a mutation would then act on."""
        status, data = self._transport("GET", f"/repos/{self.repo}/issues/{number}", None)
        if status >= 400 or not isinstance(data, dict):
            raise DegradedWriteError(f"GitHub returned {status} reading issue #{number}")
        return data

    def edit_title(self, number: int, title: str) -> None:
        """Set the Issue's title (a whole-Issue PATCH). Kept on this light client deliberately
        (StarshipSuperjam/engine-template#937) so the on:issues kind reconciler need not import
        telemetry.GitHubIssues' heavy stack for a one-field write. The title is sent as a JSON body value (never
        interpolated into a shell or a URL), so a title carrying markup or a marker cannot break out."""
        status, _ = self._transport("PATCH", f"/repos/{self.repo}/issues/{number}", {"title": title})
        if status >= 400:
            raise DegradedWriteError(f"GitHub returned {status} editing the title of issue #{number}")
