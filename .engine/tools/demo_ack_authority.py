#!/usr/bin/env python3
"""demo_ack_authority — prove the guardrail-ack operator-authority binding (StarshipSuperjam/engine-template#958)
end to end, over faked GitHub payloads and status pages (no network). It drives the REAL code on both legs —
`ack_status.main()` (the writer) and `weakening_guard._latest_engine_ack_state` (the reader) — never a stub:

  - TEAM tier — a label applied by the engine's OWN identity is REFUSED (posts engine-ack=failure); a label
    applied by a DISTINCT operator mints engine-ack=success, annotated `[operator]`.
  - SOLO tier — a label is accepted (one-step consent preserved) but annotated `[shared credential]`: a
    deliberate action, not an identity-verified one.
  - READER — a `success` status POSTed by an UNTRUSTED creator (a builder minting it directly, bypassing the
    workflow) is IGNORED; only a status GitHub stamped as the trusted `github-actions[bot]` is counted.

Why a driven demo and not a live one: the engine's own home repo is SOLO, so team-tier REFUSAL cannot be
exercised against live GitHub there. This narrates the real code over stand-in inputs so the behaviour is
visible anyway. The live-demonstrable piece is the SOLO `[shared credential]` annotation on a real status.

RETURNS NON-ZERO if any invariant is broken (the falsification can fail).
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ack_status  # noqa: E402
import github_client  # noqa: E402
import protection_guard  # noqa: E402
import weakening_guard  # noqa: E402

_TEAM = {"identity": "team", "engine_identity": {"login": "engine-bot"}, "home_repository": "o/r"}
_SOLO = {"identity": "solo", "home_repository": "o/r"}
_BOT = {"login": "github-actions[bot]"}  # the trusted creator (the default GITHUB_TOKEN's stamped identity)


@contextlib.contextmanager
def _writer_env(manifest, sender):
    """Run ack_status.main() for a `labeled` guardrail-ack event from `sender`, with the base manifest set to
    `manifest` and the GitHub transport faked. Yields the list of recorded POST bodies."""
    tmp = tempfile.mkdtemp(prefix="demo-ack-authority-")
    posts: list = []
    saved = (github_client.json_request, protection_guard._ENGINE_DIR, dict(os.environ))
    try:
        eng = os.path.join(tmp, "engine")
        os.makedirs(eng, exist_ok=True)
        with open(os.path.join(eng, "engine.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh)
        protection_guard._ENGINE_DIR = eng
        event = {"action": "labeled", "label": {"name": "guardrail-ack"}, "sender": sender,
                 "pull_request": {"number": 7, "head": {"sha": "deadbeef"}}}
        path = os.path.join(tmp, "event.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(event, fh)
        os.environ.update({"GITHUB_EVENT_PATH": path, "GITHUB_REPOSITORY": "o/r", "GITHUB_TOKEN": "t"})

        def fake(method, api_path, tok, *, user_agent, body=None):
            posts.append(body or {})
            return (201, None)
        github_client.json_request = fake
        with contextlib.redirect_stdout(io.StringIO()):
            ack_status.main()
        yield posts
    finally:
        github_client.json_request = saved[0]
        protection_guard._ENGINE_DIR = saved[1]
        os.environ.clear()
        os.environ.update(saved[2])
        __import__("shutil").rmtree(tmp, ignore_errors=True)


def _reader_state(statuses):
    """The reader's verdict for a head carrying `statuses` (list of status dicts), transport faked."""
    saved = weakening_guard.get_page
    try:
        weakening_guard.get_page = lambda url, token, **kw: (statuses, None)
        return weakening_guard._latest_engine_ack_state("o/r", "HEAD", "t")
    finally:
        weakening_guard.get_page = saved


def demo() -> int:
    failures: list = []

    def check(label, ok, detail=""):
        mark = "ok" if ok else "BROKEN"
        print(f"  [{mark}] {label}{(' — ' + detail) if detail and not ok else ''}")
        if not ok:
            failures.append(label)

    print("Writer — TEAM tier:")
    with _writer_env(_TEAM, {"login": "engine-bot", "type": "User"}) as posts:
        body = posts[0] if posts else {}
        check("the engine's own identity self-acking is REFUSED (engine-ack=failure)",
              body.get("state") == "failure" and "engine's own identity" in body.get("description", ""),
              str(body))
    with _writer_env(_TEAM, {"login": "alice", "type": "User"}) as posts:
        body = posts[0] if posts else {}
        check("a distinct operator mints engine-ack=success annotated [operator]",
              body.get("state") == "success" and "[operator]" in body.get("description", ""),
              str(body))

    print("Writer — SOLO tier:")
    with _writer_env(_SOLO, {"login": "alice", "type": "User"}) as posts:
        body = posts[0] if posts else {}
        check("a label is accepted (success) but annotated [shared credential]",
              body.get("state") == "success" and "[shared credential]" in body.get("description", ""),
              str(body))

    print("Reader — trusted-creator filter:")
    minted = [{"context": "engine-ack", "state": "success", "creator": {"login": "attacker"}}]
    check("a success minted by an UNTRUSTED creator is ignored (reads as un-acked)",
          _reader_state(minted) is None, repr(_reader_state(minted)))
    trusted = [{"context": "engine-ack", "state": "success", "creator": _BOT}]
    check("a success stamped by the trusted github-actions[bot] is counted",
          _reader_state(trusted) == "success", repr(_reader_state(trusted)))
    shadowed = [{"context": "engine-ack", "state": "success", "creator": {"login": "attacker"}},
                {"context": "engine-ack", "state": "success", "creator": _BOT}]
    check("a minted success in front of a trusted one does not mask it",
          _reader_state(shadowed) == "success", repr(_reader_state(shadowed)))

    if failures:
        print(f"\nDEMO FAILED — {len(failures)} invariant(s) broken: " + "; ".join(failures))
        return 1
    print("\nDEMO PASSED — the writer refuses a team self-ack and annotates by tier, and the reader trusts only "
          "the ack workflow's bot-stamped status (#958).")
    return 0


def main(argv: list) -> int:
    if argv and argv[0] != "demo":
        print(f"usage: {os.path.basename(__file__)} [demo]", file=sys.stderr)
        return 2
    return demo()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
