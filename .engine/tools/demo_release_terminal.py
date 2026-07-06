#!/usr/bin/env python3
"""Behavioural demonstration of the terminal-cut publisher (release_terminal.py).

Run it and watch the REAL publish + announce logic act on a faked GitHub boundary — nothing here
reimplements the tool; only the network is a stub, and every step asserts an outcome that can FAIL:

  1. FRESH PUBLISH — first release (no prior release): the tag is created at the EXACT merge commit via
     the Git Data API, the GitHub Release is published, and the success is commented onto the merged PR.
  2. IDEMPOTENT RE-RUN — running again over the now-published state is a clean no-op: no second Release,
     reported "already released" (a re-run completes/repeats safely, never double-publishes).
  3. WRONG-COMMIT REFUSAL — a tag for this version already on a DIFFERENT commit is refused, never
     overwritten; nothing is published.
  4. CREATE-FAILURE RECOVERY — when the Release step fails after the tag was made, the tool reports the
     split-brain in plain language with a re-run recovery, and comments that recovery onto the PR.

  uv run --directory .engine -- python tools/demo_release_terminal.py

Offline, no real-repo mutation — the first real publish is the 0.1.0 beta cut.
"""
import release_terminal as rt

COMMIT = "a" * 40
OTHER = "b" * 40


class _FakeGitHub:
    """A stateful GitHub transport stub: (method, path, body) -> (status, json). Mutable, so a create is
    visible to a later read and the real idempotency/convergence logic runs end to end."""

    def __init__(self, *, latest="__none__", tags=None, releases=None, create_release_status=201):
        self.latest = latest
        self.tags = dict(tags or {})
        self.releases = set(releases or [])
        self.create_release_status = create_release_status
        self.calls = []

    def transport(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path.endswith("/releases/latest"):
            return (404, None) if self.latest == "__none__" else (200, {"tag_name": self.latest})
        if "/git/ref/tags/" in path:
            tag = path.rsplit("/", 1)[1]
            return (200, {"object": {"sha": self.tags[tag], "type": "commit"}}) if tag in self.tags else (404, None)
        if method == "POST" and path.endswith("/git/refs"):
            self.tags[body["ref"].rsplit("/", 1)[1]] = body["sha"]
            return 201, None
        if "/releases/tags/" in path:
            tag = path.rsplit("/", 1)[1]
            return (200, {"tag_name": tag}) if tag in self.releases else (404, None)
        if method == "POST" and path.endswith("/releases"):
            if self.create_release_status in (200, 201):
                self.releases.add(body["tag_name"])
            return self.create_release_status, None
        if method == "POST" and "/comments" in path:
            return 201, {"id": 1}
        raise AssertionError(f"unexpected call {method} {path}")

    def created_refs(self):
        return [b for (m, p, b) in self.calls if m == "POST" and p.endswith("/git/refs")]

    def created_releases(self):
        return [b for (m, p, b) in self.calls if m == "POST" and p.endswith("/releases")]

    def comments(self):
        return [b["body"] for (m, p, b) in self.calls if m == "POST" and "/comments" in p]


def _client(fake):
    return rt.TerminalCutClient("acme/engine-home", "tok", transport=fake.transport)


def main() -> int:
    ok = True

    # 1. FRESH PUBLISH -----------------------------------------------------------------------------
    fake = _FakeGitHub(latest="__none__")
    r1 = rt.run(_client(fake), "0.1.0", COMMIT, pr_number=7)
    tag_ref = fake.created_refs()[0] if fake.created_refs() else {}
    print("1. FRESH PUBLISH (first release)")
    print(f"   published={r1['published']}  tag={r1.get('tag')}")
    print(f"   tag pinned to the exact merge commit = {tag_ref.get('sha') == COMMIT}")
    print(f"   Release published = {len(fake.created_releases()) == 1}")
    print(f"   commented on the merged PR = {fake.comments()[:1] and 'is now released' in fake.comments()[0]}")
    ok &= (r1["published"] and r1["tag"] == "v0.1.0" and tag_ref.get("sha") == COMMIT
           and len(fake.created_releases()) == 1
           and bool(fake.comments()) and "is now released" in fake.comments()[0])

    # 2. IDEMPOTENT RE-RUN -------------------------------------------------------------------------
    fake.latest = "v0.1.0"                       # the state after step 1 is now the latest release
    r2 = rt.run(_client(fake), "0.1.0", COMMIT, pr_number=7)
    print("\n2. IDEMPOTENT RE-RUN (same version, already published)")
    print(f"   published={r2['published']}  reason={r2.get('reason')}")
    print(f"   no second Release created = {len(fake.created_releases()) == 1}")
    ok &= (r2["published"] and r2["reason"] == "already-published" and len(fake.created_releases()) == 1)

    # 3. WRONG-COMMIT REFUSAL ----------------------------------------------------------------------
    conflict = _FakeGitHub(latest="__none__", tags={"v0.1.0": OTHER})
    r3 = rt.publish(_client(conflict), "0.1.0", COMMIT)
    print("\n3. WRONG-COMMIT REFUSAL (a v0.1.0 tag already on a different commit)")
    print(f"   published={r3['published']}  reason={r3.get('reason')}")
    print(f"   nothing created/overwritten = {conflict.created_refs() == [] and conflict.created_releases() == []}")
    ok &= (not r3["published"] and r3["reason"] == "tag-conflict"
           and conflict.created_refs() == [] and conflict.created_releases() == [])

    # 4. CREATE-FAILURE RECOVERY -------------------------------------------------------------------
    failing = _FakeGitHub(latest="__none__", create_release_status=500)
    r4 = rt.run(_client(failing), "0.1.0", COMMIT, pr_number=9)
    print("\n4. CREATE-FAILURE RECOVERY (tag made, Release step fails)")
    print(f"   published={r4['published']}  reason={r4.get('reason')}")
    print(f"   tag was created (the split-brain) = {len(failing.created_refs()) == 1}")
    print(f"   plain recovery names a re-run = {'re-run' in (r4.get('recovery', '').lower())}")
    print(f"   failure commented on the PR = {failing.comments()[:1] and 'did not finish' in failing.comments()[0]}")
    ok &= (not r4["published"] and r4["reason"] == "release-create-failed"
           and len(failing.created_refs()) == 1 and "re-run" in r4.get("recovery", "").lower()
           and bool(failing.comments()) and "did not finish" in failing.comments()[0])

    print("\n" + ("DEMO PASSED: the publisher tagged the exact merge commit, published the Release, no-op'd an "
                  "already-published re-run, refused a wrong-commit tag without overwriting it, and surfaced a "
                  "failed publish with a plain re-run recovery on the pull request."
                  if ok else "DEMO DID NOT BEHAVE AS EXPECTED — see above."))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
