#!/usr/bin/env python3
"""Behavioral demo — the upgrade-overwrite disclosure, end to end.

Exercises the REAL tool logic (`overwritten_paths` → `compose_comment` → `reconcile`, the deployed gate, the
engine-authored-PR exemption, and the render sanitizer) against a fake GitHub transport and a fixture
overwrite set — faking ONLY the GitHub boundary and the tree-read, the way a workflow's real inputs arrive.
It can FAIL: each scenario asserts the observable behaviour, so a regression breaks the run.

Shows:
  (a) a deployed repo where the pull request edits an overlay file → one plain comment naming the file, the
      durable upstream home, and stating it does not block the merge;
  (b) a deployed repo where the pull request touches only a PRESERVED file → silence (no comment);
  (c) a rename to an attacker-chosen name → the crafted name is sanitized, never injected;
  (d) an engine-authored update PR / a self-hosting repo → the disclosure is off.

Fate: construction evidence, pinned by `test_overlay_disclosure.py`; retires with the build-conformance
harness at v1 (it does not travel into a generated repo).
"""
import overlay_disclosure as od

# The stand-in overwrite set (what module_manager.overlay_replace_paths() returns from a real deployed tree):
# an engine tool + a module manifest (the manifest category the overlay overwrites). A PRESERVED file
# (operator config, the CLAUDE.md fence) is simply NOT in this set, so it can never be warned about. The
# crafted rename target is present (a rename put it into the tree, so it is a set member).
CRAFTED = ".engine/tools/a`b](http://evil.com).py"
OVERWRITE = {".engine/tools/boot.py", ".engine/modules/core/manifest.json", CRAFTED}
HOME = "acme/engine-home"


class _FakeGitHub:
    """Records posts/edits; answers list_comments from its own store — the injected GitHub boundary. A posted
    comment is stored bot-authored, as the real Actions token would be."""

    def __init__(self, comments=None):
        self.comments = list(comments or [])
        self.posted = []
        self._id = 1000

    def __call__(self, method, path, body=None):
        if method == "GET" and "/comments" in path:
            return 200, list(self.comments)
        if method == "POST" and path.endswith("/comments"):
            self._id += 1
            self.comments.append({"id": self._id, "body": body["body"], "user": {"type": "Bot"}})
            self.posted.append(body["body"])
            return 201, {"id": self._id}
        if method == "PATCH" and "/comments/" in path:
            cid = int(path.rsplit("/", 1)[-1])
            for c in self.comments:
                if c["id"] == cid:
                    c["body"] = body["body"]
            return 200, {"id": cid}
        return 200, None


def _run(title, changed, overwrite):
    """Run the REAL filter + comment + reconcile against a fresh fake transport. Returns (status, fake)."""
    orig = od.module_manager.overlay_replace_paths
    od.module_manager.overlay_replace_paths = lambda: overwrite
    try:
        paths = od.overwritten_paths(changed)
        fake = _FakeGitHub()
        status = od.reconcile(od._Comments("acme/product", "tok", transport=fake), 7, paths, HOME)
    finally:
        od.module_manager.overlay_replace_paths = orig
    print(f"\n=== {title} ===")
    print(f"  changed files : {[c['filename'] for c in changed]}")
    print(f"  would overwrite: {paths or '(none)'}")
    print(f"  reconcile     : {status}; comments posted = {len(fake.posted)}")
    if fake.posted:
        print("  ---- comment ----")
        for line in fake.posted[0].splitlines():
            print(f"  | {line}")
    return status, fake


def main() -> int:
    # (a) deployed + an overlay-file edit → a comment naming the file + the home.
    status, fake = _run(
        "Deployed repo, a change to an engine file the update overwrites",
        [{"filename": ".engine/tools/boot.py", "status": "modified"}], OVERWRITE)
    assert status == "posted", status
    body = fake.posted[0]
    assert ".engine/tools/boot.py" in body, "the comment must name the file"
    assert "does not block your merge" in body, "the comment must say it is non-blocking"
    assert "upstream" in body and HOME in body, "the comment must route to the named durable home"

    # (b) deployed + only a preserved carve-out edit → silence.
    status, fake = _run(
        "Deployed repo, a change to only a PRESERVED file (operator config)",
        [{"filename": ".engine/operator-overrides.json", "status": "modified"}], OVERWRITE)
    assert status == "clean", status
    assert not fake.posted, "a preserved file must never draw a comment"

    # (c) a rename to an attacker-chosen name → the crafted name is SANITIZED, never injected.
    status, fake = _run(
        "Deployed repo, an engine file renamed to an attacker-chosen name",
        [{"filename": CRAFTED, "previous_filename": ".engine/tools/boot.py", "status": "renamed"}], OVERWRITE)
    assert status == "posted", status
    assert "http://evil.com" not in fake.posted[0], "a crafted link must never render"
    assert "`b]" not in fake.posted[0], "a backtick break-out must never render"

    # (d) engine-authored update PR, and self-hosting repo → the disclosure is off.
    print("\n=== The disclosure is off for the engine's own flows ===")
    exempt = od._is_engine_authored({"pull_request": {"head": {"ref": "engine-update-v0.2.0"}}})
    print(f"  engine-update PR exempt = {exempt}")
    assert exempt is True, "an engine-authored update PR must be exempt"
    orig_home, orig_slug = od.module_manager._home_repository, od.boot.repo_slug
    od.module_manager._home_repository = lambda: "acme/product"
    od.boot.repo_slug = lambda: "acme/product"
    try:
        print(f"  self-hosting is_deployed() = {od.is_deployed()}")
        assert od.is_deployed() is False, "a repo that is its own home must be silent"
    finally:
        od.module_manager._home_repository = orig_home
        od.boot.repo_slug = orig_slug

    print("\nAll disclosure scenarios behaved as expected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
