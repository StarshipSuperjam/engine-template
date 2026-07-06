#!/usr/bin/env python3
"""Release publisher — the terminal cut (wbs/release-process.md §4 step 5 + §6).

The complement to release_cut.py: where `release_cut` decides + records the next version into the
manifests (the produce side that lands on `main` through the maintainer's merged release PR), THIS
tool runs AFTER that merge and publishes the version — a git tag and a GitHub Release **at the exact
reviewed merge commit** — then tells the maintainer, in plain language on the PR he just merged, that
it happened (or that it did not finish, and how to finish it). It is driven by
`.github/workflows/release-publish.yml` on a `release/*` PR merge.

Why the two halves are separate tools: they run at different points in the merge lifecycle and under
different trust — `release_cut` runs pre-merge under the release PAT; this runs post-merge under the
default `GITHUB_TOKEN` on a commit already trusted (it landed on protected `main`). They share no
state, so folding publish into `release_cut` would couple two things that must stay separable.

The §6 invariants this enforces:
  * The tag lands on the EXACT reviewed merge commit. `POST /releases` with `target_commitish` is
    documented "unused if the git tag already exists," so a dangling tag at a wrong commit would be
    silently honored — defeating the guarantee. Instead the tag is created via the Git Data API
    (`POST /git/refs`, pinned to the merge SHA — the `backup_vault._create_tag` primitive), and an
    existing tag's REAL commit is resolved (`/git/ref/tags/…`, annotated tags dereferenced) and
    compared: a tag on a different commit is REFUSED, never overwritten.
  * Atomic-or-loudly-incomplete. Tag-create and Release-create are two steps; a re-run CONVERGES —
    keyed on the tag's real resolved SHA it completes a half-done publish (tag made, Release missing)
    rather than double-failing, and a fully-done publish is a clean no-op. Every non-success path exits
    non-zero with a plain-language recovery (no traceback) AND posts that recovery to the merged PR,
    because the Actions run log is not where a non-engineer looks after merging.
  * The version actually moved. Before publishing, the version is checked strictly-greater than the
    repo's current `/releases/latest` (first cut — no latest — is allowed). This makes "it moved" a
    real gate, not an accident of the idempotency probe, and refuses an arbitrary/typo version or a
    stale out-of-order cut that would otherwise become the release every instance auto-pulls.

The write reuses the guardrail-safe seam: requests are built through the shared `github_client`
(carrying the off-host guard) and executed by this tool's own `urlopen` with a `(status, json)` return
— the `issue_conformance_ci` / `telemetry` idiom — so `github_client` (guardrail-class) is untouched.
The version grammar / ordering / gate-path prose are reused from `release_cut`, one home, no drift.

CLI (driven by the workflow; the network boundary is the injectable `transport` seam a test replaces):
  python tools/release_terminal.py publish --commit <merge_sha> --pr <number>
    env: GITHUB_REPOSITORY (owner/repo to publish into), GITHUB_TOKEN (contents+PR write)
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

import github_client
import module_coherence
import release_cut

USER_AGENT = "engine-release-terminal"
SENTINEL = release_cut.SENTINEL


class PublishError(Exception):
    """A GitHub call the publish depends on failed in a way that is NOT a clean 404 — an unreachable host
    or an unexpected status. NEVER swallowed as success: it surfaces as a loud non-zero exit + a plain
    recovery on the PR, so a failed-or-unverifiable publish is visible, never a silent split-brain."""


# --------------------------------------------------------------------------- the GitHub boundary
class TerminalCutClient:
    """The publish client over one repo + token. Mirrors issue_conformance_ci's injectable-transport seam:
    `transport(method, path, body) -> (status, json|None)` is injectable, so the demo and tests fake ONLY
    the network and run the REAL publish logic offline. Requests are built through the shared
    `github_client` (the off-host guard); an unreachable host raises PublishError (a publish that cannot be
    verified is loud, never assumed done)."""

    def __init__(self, repo: str, token: str, *, transport=None):
        self.repo = repo
        self.token = token
        self._transport = transport or self._http

    def _http(self, method: str, path: str, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = github_client.request(path, self.token, user_agent=USER_AGENT, method=method, data=data)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:            # 4xx/5xx — surface the status, never swallow
            return exc.code, None
        except urllib.error.URLError as exc:              # unreachable host — a publish we cannot verify
            raise PublishError(f"GitHub is unreachable: {exc}") from exc

    def latest_release_tag(self) -> str | None:
        """The repo's latest published Release tag, or None when there is NO published release yet (a clean
        404 — the first-cut case). A non-404 failure raises (an unverifiable 'is this newer?' must be loud)."""
        status, data = self._transport("GET", f"/repos/{self.repo}/releases/latest", None)
        if status == 404:
            return None
        if not isinstance(status, int) or status >= 400:
            raise PublishError(f"could not read the latest release (GitHub returned {status})")
        return (data or {}).get("tag_name")

    def tag_commit_sha(self, tag: str) -> str | None:
        """The commit SHA a tag ref resolves to, or None when the tag does not exist (a clean 404). An
        annotated tag (`object.type == 'tag'`) is dereferenced to its target commit, so the returned SHA is
        always a commit — the value compared against the merge commit."""
        status, data = self._transport("GET", f"/repos/{self.repo}/git/ref/tags/{tag}", None)
        if status == 404:
            return None
        if not isinstance(status, int) or status >= 400 or not data:
            raise PublishError(f"could not read the tag {tag} (GitHub returned {status})")
        obj = data.get("object") or {}
        sha, kind = obj.get("sha"), obj.get("type")
        if kind == "tag":                                 # annotated tag -> deref to the commit it points at
            s2, d2 = self._transport("GET", f"/repos/{self.repo}/git/tags/{sha}", None)
            if not isinstance(s2, int) or s2 >= 400 or not d2:
                raise PublishError(f"could not dereference the annotated tag {tag} (GitHub returned {s2})")
            sha = (d2.get("object") or {}).get("sha")
        return sha

    def create_tag_ref(self, tag: str, commit_sha: str) -> "int | None":
        """Create refs/tags/<tag> -> commit_sha via the Git Data API (pins the EXACT commit; the
        backup_vault._create_tag primitive). Returns the HTTP status: 200/201 created; 409/422 a name
        collision (a ref already exists — the caller re-resolves and compares, never overwrites)."""
        status, _ = self._transport("POST", f"/repos/{self.repo}/git/refs",
                                     {"ref": f"refs/tags/{tag}", "sha": commit_sha})
        return status

    def release_exists(self, tag: str) -> bool:
        """Whether a published Release object exists for `tag` (the artifact the updater's `/releases/latest`
        reads — a bare tag is invisible to it, so this, not tag-existence, is the 'already published' test)."""
        status, _ = self._transport("GET", f"/repos/{self.repo}/releases/tags/{tag}", None)
        if status == 404:
            return False
        if not isinstance(status, int) or status >= 400:
            raise PublishError(f"could not read the release for {tag} (GitHub returned {status})")
        return True

    def create_release(self, tag: str, name: str, body: str) -> "int | None":
        """Create the published Release for an ALREADY-EXISTING tag (so `target_commitish` is moot — the tag
        is already pinned to the reviewed commit). Returns the HTTP status."""
        status, _ = self._transport("POST", f"/repos/{self.repo}/releases",
                                     {"tag_name": tag, "name": name, "body": body})
        return status

    def post_pr_comment(self, number: int, body: str) -> "int | None":
        """Post a plain-language comment to the merged release PR (a PR is an Issue for the comments API).
        Returns the HTTP status; the caller treats a failure as a legibility gap to note, not a publish
        failure (the published Release is the durable success artifact)."""
        status, _ = self._transport("POST", f"/repos/{self.repo}/issues/{number}/comments", {"body": body})
        return status


# --------------------------------------------------------------------------- release notes + PR comment prose
def _release_notes(tag: str) -> str:
    """The published Release's notes — a human-only surface (no consumer reads the body; the updater reads
    only the tag + the tarball). Minimal and plain: the version and the §6 readiness line, in the same voice
    as the release PR. No internal vocabulary (§8/§12) — 'engine version vX.Y.Z', never 'terminal cut'."""
    return (f"Engine version {tag}.\n\n"
            f"{release_cut._gate_path_line('sub-bar')}")


def _comment_body(result: dict) -> str:
    """The plain-language comment posted back to the merged release PR — the legible surface a non-engineer
    actually sees after merging (the Actions run log is not). One home for the release/failure prose; the
    same `message`/`recovery` the CLI prints, so the two never diverge."""
    tag = result.get("tag") or "the new version"
    if result.get("published"):
        if result.get("reason") == "already-published":
            return (f"**Engine version {tag} is already released.** This run found it already published and "
                    f"made no change — nothing to do.")
        return (f"**Engine version {tag} is now released.** Your instances can upgrade to it. "
                f"You do not need to do anything else.")
    if result.get("reason") in ("tag-create-failed", "release-create-failed"):
        return (f"**The version was merged, but publishing {tag} did not finish.** {result.get('message', '')} "
                f"You can re-run this workflow run to finish it — re-running is safe, and nothing else you did "
                f"is lost.")
    if result.get("reason") == "nothing-to-publish":
        return (f"**No release was published.** {result.get('message', '')} This is expected if this branch "
                f"did not set a new engine version.")
    # a loud refusal (not newer than the latest release, or a tag already on a different commit)
    return (f"**This merge did not publish a release.** {result.get('message', '')} "
            f"{result.get('recovery', '')}").strip()


# --------------------------------------------------------------------------- the publish decision
def publish(client: TerminalCutClient, engine_release: str, commit_sha: str) -> dict:
    """Publish `engine_release` at `commit_sha` (idempotent, exact-commit, raise-only). Returns a result
    dict carrying `published`, a `reason`, the `tag`, and plain-language `message`/`recovery`. Writes only
    a git tag + a Release; refuses loudly (never silently) on any doubt."""
    # 1. version grammar first — a malformed string (which may itself contain a '-') is 'invalid', distinct
    #    from a well-formed pre-release. In the real flow release_cut.apply already enforced this; the check
    #    here means a hand-edited/garbage engine.json is named plainly rather than mis-called a pre-release.
    if not release_cut._valid_version(engine_release):
        return {"published": False, "reason": "invalid-version", "tag": None,
                "message": f"the recorded engine version '{engine_release}' is not a valid version number.",
                "recovery": "the version in .engine/engine.json must be a dotted-number version like 0.1.0."}

    # 2. sentinel / pre-release -> a loud no-op (a stray release/* merge that never ran the cut writer, or a
    #    well-formed pre-release that is not a real release). Not a failure — there is nothing to publish.
    if engine_release == SENTINEL or release_cut._is_prerelease(engine_release):
        return {"published": False, "reason": "nothing-to-publish", "tag": None,
                "message": f"the recorded engine version is '{engine_release}', which is not a released "
                           f"version, so nothing was published."}

    tag = f"v{engine_release}"

    # 3. the version must have actually MOVED — at least as high as the current latest release (first cut, no
    #    latest, is allowed). A STRICTLY OLDER version is refused (an arbitrary/typo version or a stale
    #    out-of-order cut that would otherwise become the release every instance auto-pulls). A version EQUAL
    #    to the latest is allowed through to the idempotency checks below: it is either a clean re-run of this
    #    same publish (already-published) or the same version re-cut on a different commit (a tag conflict) —
    #    both handled there, so this guard must not pre-empt the idempotent no-op with a false "not newer".
    latest = client.latest_release_tag()
    if latest is not None:
        lb = _bare(latest)
        newer = release_cut._strictly_greater(engine_release, lb)
        same = release_cut._release_tuple(engine_release) == release_cut._release_tuple(lb)
        if not (newer or same):
            return {"published": False, "reason": "not-newer", "tag": tag,
                    "message": f"version {tag} is older than the current latest release {latest}, so it was "
                               f"not published (a release can only ever go up).",
                    "recovery": "if this should be a new release, cut it again with a higher version number."}

    # 4. the tag must land on the EXACT reviewed merge commit (Git Data API, not target_commitish).
    existing_sha = client.tag_commit_sha(tag)
    if existing_sha is None:
        status = client.create_tag_ref(tag, commit_sha)
        if status in (200, 201):
            existing_sha = commit_sha                     # created at the exact commit
        elif status in (409, 422):                        # lost a race — a ref appeared; re-resolve + compare
            existing_sha = client.tag_commit_sha(tag)
            if existing_sha is None:
                return {"published": False, "reason": "tag-create-failed", "tag": tag,
                        "message": f"the tag {tag} could not be created (GitHub returned {status}).",
                        "recovery": "re-run this workflow run to finish publishing."}
        else:
            return {"published": False, "reason": "tag-create-failed", "tag": tag,
                    "message": f"the tag {tag} could not be created (GitHub returned {status}).",
                    "recovery": "re-run this workflow run to finish publishing."}
    if existing_sha != commit_sha:                        # a tag for this version exists on a DIFFERENT commit
        return {"published": False, "reason": "tag-conflict", "tag": tag,
                "message": f"a tag {tag} already exists on a different commit ({existing_sha[:12]}) than this "
                           f"merge ({commit_sha[:12]}), so nothing was published — the existing tag was left "
                           f"untouched.",
                "recovery": "resolve which commit should carry this version before publishing it."}

    # 5. ensure the published Release (the tag now exists at the correct commit). Idempotent: an existing
    #    Release is a clean no-op — a re-run after a half-done publish completes it here.
    if client.release_exists(tag):
        return {"published": True, "reason": "already-published", "tag": tag, "commit": commit_sha,
                "message": f"engine version {tag} is already released."}
    status = client.create_release(tag, tag, _release_notes(tag))
    if status not in (200, 201):
        return {"published": False, "reason": "release-create-failed", "tag": tag,
                "message": f"the tag {tag} was created at this commit, but the release could not be published "
                           f"(GitHub returned {status}).",
                "recovery": "re-run this workflow run to finish publishing the release."}
    return {"published": True, "reason": "published", "tag": tag, "commit": commit_sha,
            "message": f"engine version {tag} is now released."}


def _bare(tag: str) -> str:
    """A tag string with a single leading 'v' stripped for version comparison (the tags are v-prefixed; the
    ordering in release_cut compares bare version numbers)."""
    return tag[1:] if tag.startswith("v") else tag


def run(client: TerminalCutClient, engine_release: str, commit_sha: str, pr_number: "int | None") -> dict:
    """Publish, then announce the outcome on the merged PR (both success AND failure — the §6 legibility
    surface). A comment failure is NOTED, never allowed to flip the publish verdict: the published Release
    is the durable success artifact, and a missing comment is a legibility gap, not a publish failure."""
    result = publish(client, engine_release, commit_sha)
    if pr_number:
        try:
            status = client.post_pr_comment(pr_number, _comment_body(result))
            result["announced"] = isinstance(status, int) and status < 400
            if not result["announced"]:
                result["announce_error"] = f"GitHub returned {status} posting the comment"
        except PublishError as exc:
            result["announced"] = False
            result["announce_error"] = str(exc)
    return result


# --------------------------------------------------------------------------- CLI
def _exit_code(result: dict) -> int:
    """0 for a published release or a deliberate no-op; 1 for any refusal or unfinished publish (a loud stop
    the maintainer must see — a red run plus the PR comment)."""
    if result.get("published") or result.get("reason") == "nothing-to-publish":
        return 0
    return 1


def _cmd_publish(args) -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not repo:
        print("CONFIG ERROR: GITHUB_REPOSITORY is not set; cannot tell which repository to publish into.",
              file=sys.stderr)
        return 2
    if not token:
        print("CONFIG ERROR: GITHUB_TOKEN is not set; cannot authenticate to publish the release.",
              file=sys.stderr)
        return 2
    engine = module_coherence.load_engine_manifest()
    if engine is None:
        print("CONFIG ERROR: the engine manifest (.engine/engine.json) is missing; nothing to publish.",
              file=sys.stderr)
        return 2
    engine_release = engine.get("engine_release", SENTINEL)
    pr_number = int(args.pr) if args.pr else None

    client = TerminalCutClient(repo, token)
    result = run(client, engine_release, args.commit, pr_number)

    # plain-language outcome to the run log (the PR comment carries the same words to the maintainer)
    print(result.get("message", ""))
    if result.get("recovery"):
        print(f"To fix: {result['recovery']}")
    if result.get("announce_error"):
        print(f"(could not post the outcome to the pull request: {result['announce_error']})", file=sys.stderr)
    return _exit_code(result)


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(prog="release_terminal.py",
                                 description="Publish the tag + GitHub Release for a merged release.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pub = sub.add_parser("publish", help="publish the tag + Release at the merged commit (idempotent)")
    pub.add_argument("--commit", required=True, help="the reviewed merge commit SHA to tag")
    pub.add_argument("--pr", help="the merged pull request number to comment the outcome onto")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "publish":
            return _cmd_publish(args)
    except Exception as exc:  # plain-language failure, never a traceback (release-process §6)
        print(f"\nRELEASE PUBLISH ERROR: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
