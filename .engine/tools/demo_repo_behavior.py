#!/usr/bin/env python3
"""Operator-runnable demo: the engine turns on the working-comfort repository settings a new project should
carry (automatic tidy-up of merged branches, the pull-request update button, dependency alerts, automatic
security-fix pull requests), reads each one FIRST and leaves an already-on setting exactly as it is, never
claims a setting is on when the read-back didn't confirm it, and discloses — in plain words — what changed,
what was already yours, and what an organization's own settings reserve.

Run: uv run --directory .engine -- python tools/demo_repo_behavior.py   (no network, no token needed)

This exercises the REAL logic (`repo_behavior.RepoBehavior`) against FAKE GitHub answers representing four
real situations. For each one you see the situation we fed in and the exact message the engine would show
you — so you can check the SAME code says "now on" in one case, "already yours, untouched" in another, and
"your organization controls this one" in a third. Nothing here touches a real project."""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import repo_behavior as rb   # noqa: E402

REPO = "you/your-project"


def _fake(*, settings, alerts_on, alerts_put, fixes_on, fixes_put):
    """A fake GitHub: `settings` is the live repo-settings dict (a PATCH mutates it, so the read-back really
    reflects the write); the Dependabot switches are stateful the same way — a successful PUT flips them."""
    state = dict(settings)
    alerts = {"on": alerts_on}
    fixes = {"on": fixes_on}

    def t(method, path, body=None):
        if path.endswith("/vulnerability-alerts"):
            if method == "PUT":
                if alerts_put[0] < 400:
                    alerts["on"] = True
                return alerts_put[0], alerts_put[1], {}
            return (204, None, {}) if alerts["on"] else (404, None, {})
        if path.endswith("/automated-security-fixes"):
            if method == "PUT":
                if fixes_put[0] < 400:
                    fixes["on"] = True
                return fixes_put[0], fixes_put[1], {}
            return 200, {"enabled": fixes["on"], "paused": False}, {}
        if method == "PATCH" and isinstance(body, dict):
            state.update(body)
            return 200, {}, {}
        if method == "GET" and path.startswith("/repos/"):
            return 200, dict(state, full_name=REPO), {}
        return 404, None, {}
    return t


_FRESH = {"delete_branch_on_merge": False, "allow_update_branch": False}
_DONE = {"delete_branch_on_merge": True, "allow_update_branch": True}


def main() -> int:
    print("REPO-BEHAVIOR DEMO — the working-comfort settings a new project should carry, honestly reported.\n")
    ok = True

    def scenario(title, transport, expect_states, *, disable_wiki=False, disable_projects=False):
        nonlocal ok
        print(f"— {title}")
        said = []
        toggles = rb.RepoBehavior(REPO, "tok", transport=transport).apply(
            announce=said.append, disable_wiki=disable_wiki, disable_projects=disable_projects)
        for line in said[0].split("\n"):
            print(f"    {line}")
        got = {t.key: t.state for t in toggles}
        good = all(got.get(k) == v for k, v in expect_states.items())
        print(f"    → outcome per setting behaves as expected: {good}\n")
        ok &= good

    scenario("A fresh project (everything off): all four are turned on and confirmed.",
             _fake(settings=_FRESH, alerts_on=False, alerts_put=(204, None),
                   fixes_on=False, fixes_put=(204, None)),
             {"delete-branch-on-merge": rb.ON, "update-branch": rb.ON,
              "dependabot-alerts": rb.ON, "dependabot-fixes": rb.ON})

    scenario("A project that already chose these settings: everything is left exactly as it was.",
             _fake(settings=_DONE, alerts_on=True, alerts_put=(204, None),
                   fixes_on=True, fixes_put=(204, None)),
             {"delete-branch-on-merge": rb.ALREADY, "update-branch": rb.ALREADY,
              "dependabot-alerts": rb.ALREADY, "dependabot-fixes": rb.ALREADY})

    scenario("An organization reserves the Dependabot switches: disclosed, never forced.",
             _fake(settings=_FRESH, alerts_on=False, alerts_put=(403, {"message": "org policy"}),
                   fixes_on=False, fixes_put=(403, {})),
             {"dependabot-alerts": rb.UNSUPPORTED, "dependabot-fixes": rb.UNSUPPORTED})

    def _down(method, path, body=None):
        raise rb.bootstrap.BootstrapError("unreachable")
    scenario("GitHub unreachable: nothing is reported on, and the message says to check or re-run.",
             _down,
             {"delete-branch-on-merge": rb.UNVERIFIED, "update-branch": rb.UNVERIFIED,
              "dependabot-alerts": rb.UNVERIFIED, "dependabot-fixes": rb.UNVERIFIED})

    _NEW = dict(_FRESH, has_wiki=True, has_projects=True)   # a fresh repo: wiki + project boards on
    scenario("A fresh project without the board-sync add-on: the wiki AND unused project boards turn off.",
             _fake(settings=_NEW, alerts_on=False, alerts_put=(204, None),
                   fixes_on=False, fixes_put=(204, None)),
             {"wiki": rb.OFF, "projects": rb.OFF},
             disable_wiki=True, disable_projects=True)

    scenario("A fresh project that KEPT the board-sync add-on: the wiki turns off, project boards stay.",
             _fake(settings=_NEW, alerts_on=False, alerts_put=(204, None),
                   fixes_on=False, fixes_put=(204, None)),
             {"wiki": rb.OFF},
             disable_wiki=True, disable_projects=False)   # projects retained -> not in the toggle set

    if not ok:
        print("DEMO UNEXPECTED: an outcome did not behave as expected.", file=sys.stderr)
        return 1
    print("DEMO OK — on, already-yours, organization-reserved, unconfirmed, and the fresh-repo turn-offs "
          "(wiki off; project boards off unless the add-on is kept) are distinct and honest.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
