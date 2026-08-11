#!/usr/bin/env python3
"""The shared per-Issue label client — the ONE injectable-transport client for GitHub label reads/writes.

WHY THIS EXISTS. More than one on:issues CI backstop needs the same handful of per-Issue label operations
over the same authenticated transport: the conformance net (`issue_conformance_ci`) ensures/adds/removes the
`needs-reauthoring` label, and the kind-label applicator (`issue_kind_label`) checks whether a GitHub-native
label exists and adds it. Copying the transport into each would create two near-identical HTTP clients that
drift on the next fix (a 404 tolerance, a timeout, a status rule). So the label operations + the injectable
`transport(method, path, body) -> (status, json|None)` seam live once, here; each tool subclasses or holds one
and adds only what is truly its own (conformance keeps its comment operations; the applicator keeps its title
mapping). The transport builds requests through the shared `github_client` (the telemetry / audit_digest
idiom), so this is the label layer above that request builder, not a second request builder.

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
    """The per-Issue label client. `transport(method, path, body) -> (status, json|None)` is injectable, so a
    demo or test fakes ONLY the network and runs the real logic. `user_agent` names the calling tool on the
    wire. Deliberately NOT telemetry.GitHubIssues — that class is engine-issue-domain shaped (label baked in,
    opens/updates whole Issues); this exposes only the per-Issue label operations a CI backstop needs."""

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
