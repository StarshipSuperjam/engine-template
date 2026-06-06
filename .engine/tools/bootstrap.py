"""Control-plane bootstrap — the permanent, re-runnable operation that turns the protected-branch
ruleset ON (provisioning permanent primitive, core slice 25a).

The branch ruleset is a SETTING, not a file, so it does not travel with "Use this template" and must be
applied once per repository by an operator-privileged actor ([control-plane] §The bootstrap contract; the
#1 trust dependency, Risk R1). The control plane locks the CONTRACT (operator-privileged actor; the default
Actions token cannot create a ruleset; consent at the authorization screen; verify-after; degrade-never-
fake; the protection floor); provisioning owns the MECHANISM, which lives here so it survives the self-
deleting instantiator and the operator can re-run it any time.

This tool is permanent and re-runnable: the instantiator (slice 27) calls it at its apply phase, the
operator verb (slice 26) wraps it, and it is idempotent on its own. It surfaces only its OWN first-run
attempt outcome; the STANDING cross-session "your safety gate is off" surfacing is boot's (already built —
boot.protected_branch_signal).

CORRECTED BUILD-SPEC LEAF (token handling): the locked spec illustrates the required permission as
`admin:repo_ruleset`. Verified against GitHub's live OAuth-scopes documentation, the rulesets REST API, and
the operator's own `gh` login, NO such scope exists; creating/editing a repository ruleset requires the
standard classic `repo` scope (which a `gh` login carries by default) or a fine-grained "Administration:
write" permission. control-plane explicitly defers token handling to a provisioning build-spec leaf, so this
tool implements the correct mechanism; the locked prose's scope name is flagged for amendment (a filed doc
note). The locked CONTRACT is unchanged.

Scope OUT of this slice (named, deferred):
  - module manager add/remove + group-scoped uv-sync derivation + the orphan-wire reverse coherence leg -> 25b
  - engine updater/upgrade + migrations + CODEOWNERS injection (deployed-repo only) -> 25c
  - de-bootstrap (drop the engine binding on clean removal) -> with its consumer, 25b/25c
  - in-place augment of a pre-existing PRODUCT ruleset (brownfield coexistence) -> a brownfield slice
  - the operator verb + boot's one-click-fix copy update -> 26
  - the second engine-scheme (spec-marker) label -> post-core product-design (core ensures only the
    engine-domain label here)
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402
import boot  # noqa: E402  (repo_slug, gh_token, PROTECTED_BRANCH — the shared GitHub-context helpers)
import protection_guard  # noqa: E402  (REQUIRED_CHECKS + missing_floor — the SINGLE home of the floor)
import telemetry  # noqa: E402  (GitHubIssues.ensure_label — the slice-18 minimal ensure this inherits)

API_ROOT = "https://api.github.com"
USER_AGENT = "engine-control-plane-bootstrap"
TEMPLATE_PATH = os.path.join(validate.ENGINE_DIR, "templates", "control-plane-bootstrap.md")

# The engine's own named ruleset. The engine maintains THIS ruleset; idempotence is decided by the
# evaluated floor (so a product ruleset already meeting the floor is a clean no-op and the engine never
# creates a duplicate). Operator-visible in repo settings, so the name is plain language.
ENGINE_RULESET_NAME = "engine: protected main"

# The classic OAuth scope that grants repository administration (creating/editing branch rulesets). See
# the module docstring: the locked spec's `admin:repo_ruleset` is not a real scope; `repo` is the correct
# classic scope (or fine-grained "Administration: write", which carries no classic scope string).
RULESET_SCOPE = "repo"


class BootstrapError(Exception):
    """A GitHub read/transport failure during bootstrap — surfaced and degraded, never swallowed."""


# ---- the protection-floor payload --------------------------------------------------------------

def floor_ruleset(name: str = ENGINE_RULESET_NAME) -> dict:
    """The ruleset object that satisfies protection_guard.missing_floor EXACTLY (verified against the
    live evaluated floor): a pull request before merging (zero required approvals — a sole owner cannot
    approve their own PR, [control-plane]; plus conversation resolution), the engine's required checks,
    no force-push, no deletion. Targets the default branch via the ~DEFAULT_BRANCH ref condition so it
    follows a rename."""
    return {
        "name": name,
        "target": "branch",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "rules": [
            {
                "type": "pull_request",
                "parameters": {
                    "required_approving_review_count": 0,
                    "required_review_thread_resolution": True,
                    "dismiss_stale_reviews_on_push": False,
                    "require_code_owner_review": False,
                    "require_last_push_approval": False,
                    "required_reviewers": [],
                    "allowed_merge_methods": ["merge", "squash", "rebase"],
                },
            },
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": False,
                    "do_not_enforce_on_create": False,
                    # The required-check names come from the SINGLE frozen home (never copied here).
                    "required_status_checks": [
                        {"context": c} for c in protection_guard.REQUIRED_CHECKS
                    ],
                },
            },
            {"type": "non_fast_forward"},
            {"type": "deletion"},
        ],
    }


# ---- operator-facing copy (the template SURFACE is primary; built-in fallbacks keep the #1 trust
#      tool working even if the template is absent/damaged — degrade-loud, never crash) -----------

# Built-in fallbacks. The plain-language SURFACE source is .engine/templates/control-plane-bootstrap.md;
# a test asserts the template carries each of these so they cannot silently drift.
FALLBACK_COPY = {
    "before-you-approve": (
        "I'm about to turn on your safety gate — the branch protection that keeps work from reaching "
        "your main branch without passing checks and your review. To do that I need permission to manage "
        "this repository's settings. GitHub will show an authorization screen asking for `repo` access — "
        "the standard 'manage my repository' permission. Approving it lets me set the protection rules; I "
        "can't grant it to myself, which is the point. Nothing changes until you approve."
    ),
    "degraded-not-admin": (
        "I couldn't turn on branch protection — this account doesn't administer the repository. Protection "
        "is not active, so work can merge unreviewed. Next step: ask whoever owns the repository to run "
        "this setup and approve the screen. I'll keep reminding you until it's on."
    ),
    "degraded-org-policy": (
        "I couldn't turn on branch protection — your organization's settings blocked the permission it "
        "needs. Protection is not active, so work can merge unreviewed. Two ways forward: ask your org "
        "admin to allow it, or switch to team mode (a separate engine identity that holds this "
        "permission). I'll keep reminding you until it's on."
    ),
    "degraded-didnt-save": (
        "The authorization screen completed but the permission didn't save (some sign-in methods do "
        "this). Protection is still off, so work can merge unreviewed. Let's try once more, or sign in "
        "again first. I'll keep reminding you until it's on."
    ),
    "applied": (
        "Your safety gate is on. The main branch now requires a pull request, passing checks, and resolved "
        "review comments before anything merges — and it can't be force-pushed or deleted."
    ),
    "already": (
        "Your safety gate is already on — nothing to change. (Safe to run any time; it never weakens "
        "protection that's already in place.)"
    ),
}

# Maps each copy key to its `##` heading in the template surface.
COPY_HEADINGS = {
    "before-you-approve": "Before you approve",
    "degraded-not-admin": "If it couldn't turn on — you don't administer this repository",
    "degraded-org-policy": "If it couldn't turn on — your organization blocks the permission",
    "degraded-didnt-save": "If it couldn't turn on — the approval didn't save",
    "applied": "When it's on",
    "already": "When it was already on",
}


def _parse_sections(text: str) -> dict:
    """Split a markdown body into {heading: body-text} by `## ` headings (frontmatter and the lead-in
    comment are ignored — only `## ` sections are read)."""
    sections: dict = {}
    current = None
    buf: list = []
    for line in text.splitlines():
        if line.startswith("## "):
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current = line[3:].strip()
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()
    return sections


def load_copy(path: str = TEMPLATE_PATH) -> dict:
    """Operator copy, the template surface preferred, built-in fallbacks where a section is missing or
    the template is unreadable. The returned dict is keyed by the stable COPY_HEADINGS keys."""
    by_heading: dict = {}
    try:
        with open(path, encoding="utf-8") as fh:
            by_heading = _parse_sections(fh.read())
    except OSError:
        by_heading = {}
    out = {}
    for key, heading in COPY_HEADINGS.items():
        body = by_heading.get(heading, "").strip()
        out[key] = body if body else FALLBACK_COPY[key]
    return out


# ---- the GitHub authorization refresh (the operator's consent screen) ---------------------------

def gh_auth_refresh(scope: str = RULESET_SCOPE) -> bool:
    """Open the operator's GitHub authorization screen to add `scope` to their `gh` login (web flow).
    The engine CANNOT grant itself the scope — the screen is the consent gate, consistent with the
    merge-as-consent model. Returns True if `gh` exited 0. Injectable for tests/demo."""
    try:
        out = subprocess.run(
            ["gh", "auth", "refresh", "-s", scope], timeout=300, check=False
        )
        return out.returncode == 0
    except Exception:  # noqa: BLE001 — missing binary / timeout / OS error -> treated as not refreshed
        return False


# ---- the result of an apply attempt -------------------------------------------------------------

class Result:
    """The outcome of an apply attempt, plain-language-renderable for the operator."""

    def __init__(self, status: str, branch: str, missing: list, cause: str | None,
                 labels_ok: bool = True):
        self.status = status          # "applied" | "already" | "degraded"
        self.branch = branch
        self.missing = missing        # floor pieces still not in force (for a degraded result)
        self.cause = cause            # "not-admin" | "org-policy" | "didnt-save" | "verify-failed" | None
        self.labels_ok = labels_ok

    def is_protected(self) -> bool:
        return self.status in ("applied", "already")


# ---- the control-plane bootstrap ----------------------------------------------------------------

class ControlPlane:
    """The control-plane bootstrap boundary. Mirrors telemetry.GitHubIssues' injectable-transport
    pattern, EXTENDED to return response headers (so token capability can be read from X-OAuth-Scopes).
    `transport(method, path, body) -> (status, json, headers)` is injectable so tests/demo replace ONLY
    the network and run the real logic."""

    def __init__(self, repo: str, token: str, transport=None, refresh_fn=None, issues=None):
        self.repo = repo
        self.token = token
        self._transport = transport or self._http
        self._refresh = refresh_fn or gh_auth_refresh
        # The label-ensure boundary, INHERITED from telemetry's first-producer mechanism. Injectable
        # so tests/demo replace the network; constructed lazily against the real transport otherwise.
        self._issues = issues

    def _http(self, method: str, path: str, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            API_ROOT + path, data=data, method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw else None), dict(resp.headers)
        except urllib.error.HTTPError as exc:           # 4xx/5xx — keep status + headers + body
            headers = dict(exc.headers) if exc.headers else {}
            try:
                body_json = json.loads(exc.read().decode("utf-8"))
            except Exception:  # noqa: BLE001 — an empty/non-JSON error body is fine
                body_json = None
            return exc.code, body_json, headers
        except urllib.error.URLError as exc:             # network unreachable — a transport failure
            raise BootstrapError(f"GitHub is unreachable: {exc}") from exc

    # -- capability -------------------------------------------------------------------------------

    def token_scopes(self) -> set | None:
        """The classic-OAuth scopes on the operator token, read from the X-OAuth-Scopes response header
        of any API call. Returns a set of scope strings, or None when the header is absent (a fine-
        grained token, or an unreachable API) — capability is then decided by probing the write."""
        try:
            _status, _data, headers = self._transport("GET", f"/repos/{self.repo}", None)
        except BootstrapError:
            return None
        raw = None
        for k, v in headers.items():
            if k.lower() == "x-oauth-scopes":
                raw = v
                break
        if raw is None:
            return None
        return {s.strip() for s in raw.split(",") if s.strip()}

    # -- reads ------------------------------------------------------------------------------------

    def floor_missing(self, branch: str) -> list:
        """The protection-floor pieces NOT in force on the branch, via the EVALUATED per-branch rules
        endpoint (the default token can read it) and protection_guard's own evaluation. Raises
        BootstrapError on an unreadable response — never a false 'protected'."""
        status, data, _ = self._transport(
            "GET", f"/repos/{self.repo}/rules/branches/{branch}", None)
        if status >= 400 or not isinstance(data, list):
            raise BootstrapError(f"could not read evaluated branch rules (status {status})")
        return protection_guard.missing_floor(data, protection_guard.REQUIRED_CHECKS)

    def engine_ruleset(self) -> dict | None:
        """The engine's own ruleset, if it already exists (matched by ENGINE_RULESET_NAME). Returns None
        when absent. Raises BootstrapError if the admin rulesets endpoint cannot be listed."""
        status, data, _ = self._transport("GET", f"/repos/{self.repo}/rulesets", None)
        if status >= 400 or not isinstance(data, list):
            raise BootstrapError(f"could not list rulesets (status {status})")
        for r in data:
            if r.get("name") == ENGINE_RULESET_NAME:
                return r
        return None

    # -- writes -----------------------------------------------------------------------------------

    def _write_floor(self, existing: dict | None):
        """Create the engine ruleset (POST) or repair it in place (PUT). Returns (status, body)."""
        payload = floor_ruleset()
        if existing:
            return self._transport(
                "PUT", f"/repos/{self.repo}/rulesets/{existing['id']}", payload)[:2]
        return self._transport("POST", f"/repos/{self.repo}/rulesets", payload)[:2]

    def ensure_labels(self) -> bool:
        """Idempotently ensure the engine-domain label exists, INHERITING the slice-18 minimal ensure
        (telemetry.GitHubIssues.ensure_label) — never re-deciding the string, never re-implementing the
        mechanism. Core ensures ONLY the engine-domain label; the spec-marker label is post-core. Best-
        effort: returns False on failure so the caller can disclose, never crashing the bootstrap."""
        try:
            issues = self._issues or telemetry.GitHubIssues(self.repo, self.token)
            issues.ensure_label()
            return True
        except Exception:  # noqa: BLE001 — DegradedReadError / transport failure -> disclose, don't crash
            return False

    # -- orchestration ----------------------------------------------------------------------------

    def apply(self, branch: str | None = None, announce=None) -> Result:
        """Idempotently ensure the protection floor is in force on the branch, then ensure the engine
        labels. `announce(text)` surfaces operator copy at the right moments (default: print)."""
        branch = branch or boot.PROTECTED_BRANCH
        copy = load_copy()
        say = announce if announce is not None else (lambda text: print(text))

        # 1. Already in force? (the idempotent no-op — a product ruleset that meets the floor counts).
        try:
            missing = self.floor_missing(branch)
        except BootstrapError:
            missing = None  # unreadable -> fall through to the capability+apply path
        if missing == []:
            labels_ok = self.ensure_labels()
            return Result("already", branch, [], None, labels_ok)

        # 2. Capability: does the operator token carry repository administration?
        scopes = self.token_scopes()
        needs_grant = scopes is not None and RULESET_SCOPE not in scopes
        if needs_grant:
            say(copy["before-you-approve"])
            self._refresh(RULESET_SCOPE)            # the operator approves the authorization screen
            scopes = self.token_scopes()            # verify-after-refresh (web flow can fail to persist)
            if scopes is not None and RULESET_SCOPE not in scopes:
                labels_ok = self.ensure_labels()
                return Result("degraded", branch, missing or [], "didnt-save", labels_ok)

        # 3. Apply: create the engine ruleset, or repair it in place.
        try:
            existing = self.engine_ruleset()
        except BootstrapError:
            existing = None
        status, _body = self._write_floor(existing)
        if status in (401, 403):
            # A fine-grained token without admin shows no classic scope; the write is the real probe.
            say(copy["before-you-approve"])
            self._refresh(RULESET_SCOPE)
            status, _body = self._write_floor(existing)
        if status >= 400:
            labels_ok = self.ensure_labels()
            cause = "not-admin" if status in (401, 403) else "verify-failed"
            return Result("degraded", branch, missing or [], cause, labels_ok)

        # 4. Verify the floor is now actually in force (never assume the write took).
        try:
            still_missing = self.floor_missing(branch)
        except BootstrapError:
            still_missing = None
        labels_ok = self.ensure_labels()
        if still_missing:
            return Result("degraded", branch, still_missing, "verify-failed", labels_ok)
        return Result("applied", branch, [], None, labels_ok)


# ---- rendering an outcome for the operator ------------------------------------------------------

def render(result: Result, copy: dict | None = None) -> str:
    """Plain-language rendering of an apply outcome for the operator."""
    copy = copy or load_copy()
    if result.status == "applied":
        msg = copy["applied"]
    elif result.status == "already":
        msg = copy["already"]
    else:  # degraded — pick the cause-matched banner
        key = {
            "not-admin": "degraded-not-admin",
            "org-policy": "degraded-org-policy",
            "didnt-save": "degraded-didnt-save",
        }.get(result.cause or "", "degraded-not-admin")
        msg = copy[key]
        if result.cause == "verify-failed" and result.missing:
            msg += "\nStill not in force: " + "; ".join(result.missing) + "."
    if not result.labels_ok:
        msg += ("\n(Note: I couldn't confirm the engine's issue label exists — I'll retry that next "
                "time I can reach GitHub.)")
    return msg


# ---- CLI ----------------------------------------------------------------------------------------

def _resolve_repo(arg_repo: str | None) -> str | None:
    return arg_repo or boot.repo_slug()


def cmd_status(args) -> int:
    """Read-only: report whether the safety gate is on for the branch (no writes, no consent screen)."""
    repo = _resolve_repo(args.repo)
    token = boot.gh_token()
    if not repo or not token:
        print("Can't check branch protection from here — no repository access is available. "
              "(This is normal on a machine without a logged-in `gh`.)")
        return 0
    cp = ControlPlane(repo, token)
    try:
        missing = cp.floor_missing(args.branch)
    except BootstrapError as e:
        print(f"Couldn't read branch protection for '{args.branch}' ({e}); treating it as not on.")
        return 0
    if not missing:
        print(f"Safety gate is ON for '{args.branch}'.")
    else:
        print(f"Safety gate is OFF for '{args.branch}': " + "; ".join(missing) + ".")
    return 0


def cmd_apply(args) -> int:
    """Turn the safety gate on for the branch (idempotent; surfaces the consent screen if needed)."""
    repo = _resolve_repo(args.repo)
    token = boot.gh_token()
    if not repo or not token:
        print("Can't turn on branch protection from here — no repository access is available. "
              "Run this where you're logged in to GitHub (`gh auth login`).")
        return 1
    cp = ControlPlane(repo, token)
    try:
        result = cp.apply(branch=args.branch)
    except BootstrapError as e:
        print(f"Couldn't reach GitHub to turn on branch protection ({e}). Nothing changed — try again "
              "when you're back online.")
        return 1
    print(render(result))
    return 0 if result.is_protected() else 1


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bootstrap",
        description="Turn the protected-branch safety gate on (the control-plane bootstrap).")
    parser.add_argument("--repo", default=None, help="owner/repo (default: derived from the git remote)")
    parser.add_argument("--branch", default=boot.PROTECTED_BRANCH, help="the protected branch")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("status", help="report whether the safety gate is on (read-only)")
    sub.add_parser("apply", help="turn the safety gate on (idempotent)")
    args = parser.parse_args(argv)
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "apply":
        return cmd_apply(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
