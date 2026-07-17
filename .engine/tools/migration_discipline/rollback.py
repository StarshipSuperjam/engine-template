#!/usr/bin/env python3
"""Migration rollback-presence inspector — the read-only `custom/script` entry for
engine/check/migration-rollback (the migration-discipline module's *soft* rollback-presence nudge).

What it does: detects the product's database-migration setup by convention, and checks whether migrations
that use a SEPARATE rollback file carry one. Two separate-file conventions are paired:

- the golang-migrate / raw-SQL `*.up.sql` <-> `*.down.sql` convention (an `*.up.sql` with no `*.down.sql`
  earns one `soft` nudge), and
- Flyway versioned vs undo migrations (`V<version>__*.sql` paired with `U<version>__*.sql`) — paired
  PER VERSION whenever the project uses undo migrations at all, so a single undo file can never mask a later
  versioned migration that has none.

When every separate-file migration is paired the check passes cleanly. When the project's migration tool is
forward-only and carries no rollback artifacts (Supabase, Prisma, community Flyway/Liquibase) or keeps each
rollback INSIDE the migration file (Rails, Django, Alembic), it emits one calm `soft` line saying so plainly
rather than passing silently. When no migrations exist at all it emits a calm `soft` no-op line.

Resolution is PRESENCE-FIRST: a separate rollback artifact, wherever it lives, is honoured over a
framework-name assumption — so a forward-only tool that nonetheless ships hand-written rollback files is
checked, not waved off. The framework name only shapes the wording of the calm line when no rollback artifact
is present.

Honest floor: detection is presence/convention-based over a PRUNED walk of the product tree — it never
descends into `.engine/` (the engine's own walled tooling), `.git`, `.venv`, `node_modules`, or build/cache
dirs (the engine/product wall and the read-only firewall), and it does not follow directory symlinks
(the safe default), so a migrations directory reached only through a symlink is not inspected. It reads only
file and directory NAMES and presence — it never reads SQL/DDL, never judges whether a migration is safe or
destructive, and never checks whether a schema change has a migration at all (the standing posture bar plus
the pull-request review cover those). It is a soft hygiene nudge, not a guarantee.

Tiers / blocking: every finding is `soft`, so this check never blocks a merge even in CI's blocking-gate
context. Read-only: it inspects names/presence only and never writes (the read-only mutation firewall).

Contract: invoked by the validator with NO arguments, it prints a finding.v1 JSON array to stdout and exits
0. A separate `demo` subcommand runs a falsifiable self-check.
"""
from __future__ import annotations
import json
import os
import re
import sys

# Make the sibling `.engine/tools/` modules importable whether imported as `migration_discipline.rollback`
# or run directly as the wired check script (the dependency_discipline idiom).
_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

import validate  # noqa: E402 — ROOT (test-redirectable) + the finding.v1 helper

# Directories never walked: the engine's own walled tooling (the engine/product wall), VCS, virtualenvs, dependency trees, and
# build/cache output. Pruning these is the firewall that keeps a recursive migration walk from mistaking a
# vendored or engine-owned migration artifact for one of the product's own (the firewall pinning.py gets free
# by being root-level only). Mirrors module_coherence.PRUNE_DIRS and extends it for product-repo noise.
_PRUNE_DIRS = {
    ".git", ".engine", ".venv", "venv", "env", "node_modules", "__pycache__", ".cache",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", "dist", "build", "target",
    ".next", ".nuxt", ".svelte-kit", "vendor", ".terraform", ".gradle",
}

# Flyway versioned migrations (V<version>__<desc>.sql) and their undo counterpart (U<version>__<desc>.sql).
# The version token must start with a digit, so an ordinary file like `Validate__x.sql` is not mistaken for a
# Flyway migration. Pairing is by the captured version token.
_FLYWAY_VERSIONED = re.compile(r"^V(\d[\w.+-]*)__.*\.sql$")
_FLYWAY_UNDO = re.compile(r"^U(\d[\w.+-]*)__.*\.sql$")

# Liquibase changelog files, matched by the conventional changelog filename (not just any name containing
# "changelog", which would over-match a hand-written `db_changelog.sql`).
_LIQUIBASE_EXTS = (".xml", ".yaml", ".yml", ".json", ".sql")

_UP_SUFFIX = ".up.sql"
_DOWN_SUFFIX = ".down.sql"


def _scan(root: str) -> dict:
    """Walk the product tree once (pruned, symlinks not followed) and collect the presence signals
    resolution needs: separate up/down SQL rollback files, Flyway versioned/undo files (with version
    tokens), a Liquibase changelog, a Django migrations package, and whether any conventional migrations
    directory exists at all. Names/presence only — no file contents are read."""
    info = {
        "up": [],              # relpaths of *.up.sql
        "down": set(),         # lower-cased relpaths of *.down.sql (for case-insensitive pairing)
        "flyway_v": [],        # (relpath, version-token) of V<ver>__*.sql files
        "flyway_u": set(),     # version-tokens of U<ver>__*.sql files
        "liquibase_changelog": False,
        "django_pkg": False,
        "migration_dir": False,
    }
    for dirpath, dirnames, filenames in os.walk(root):  # followlinks=False (default) — see honest floor
        # Prune caches / deps / engine-walled / VCS / dot dirs in place so the walk never descends into them.
        dirnames[:] = [d for d in dirnames if d not in _PRUNE_DIRS and not d.startswith(".")]
        base = os.path.basename(dirpath)
        if dirpath != root and base in ("migrations", "migrate"):
            info["migration_dir"] = True
            if "__init__.py" in filenames:
                info["django_pkg"] = True  # an <app>/migrations/ Python package — the Django convention
        for name in filenames:
            low = name.lower()
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            if low.endswith(_UP_SUFFIX):
                info["up"].append(rel)
            elif low.endswith(_DOWN_SUFFIX):
                info["down"].add(rel.lower())
            mv = _FLYWAY_VERSIONED.match(name)
            if mv:
                info["flyway_v"].append((rel, mv.group(1)))
            mu = _FLYWAY_UNDO.match(name)
            if mu:
                info["flyway_u"].add(mu.group(1))
            if low.startswith(("changelog", "db.changelog")) and low.endswith(_LIQUIBASE_EXTS):
                info["liquibase_changelog"] = True
    return info


def _expected_down(up_rel: str) -> str:
    """The rollback file a `*.up.sql` migration should be paired with."""
    return up_rel[: -len(_UP_SUFFIX)] + _DOWN_SUFFIX


def _forward_only_frameworks(root: str, info: dict) -> list:
    """Forward-only / no-built-in-rollback frameworks detected by marker — named only to word the calm line
    when NO separate rollback artifact was found. (Flyway lands here only when it ships versioned migrations
    but no undo files at all; a project that uses undo files is paired per-version above.)"""
    found = []
    if (os.path.isfile(os.path.join(root, "supabase", "config.toml"))
            or os.path.isdir(os.path.join(root, "supabase", "migrations"))):
        found.append("Supabase")
    if (os.path.isfile(os.path.join(root, "prisma", "schema.prisma"))
            or os.path.isdir(os.path.join(root, "prisma", "migrations"))):
        found.append("Prisma")
    if info["flyway_v"]:
        found.append("Flyway")
    if info["liquibase_changelog"]:
        found.append("Liquibase")
    return found


def _in_file_frameworks(root: str, info: dict) -> list:
    """Frameworks that keep each migration's rollback INSIDE the migration file (so a presence nudge cannot
    honestly inspect it)."""
    found = []
    if os.path.isfile(os.path.join(root, "alembic.ini")):
        found.append("Alembic")
    if os.path.isdir(os.path.join(root, "db", "migrate")):
        found.append("Rails")
    if os.path.isfile(os.path.join(root, "manage.py")) and info["django_pkg"]:
        found.append("Django")
    return found


def _join(names: list) -> str:
    names = list(dict.fromkeys(names))  # de-dup, preserve order
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + f", and {names[-1]}"


def _missing_rollback_message(up_rel: str, down_rel: str) -> str:
    return (
        f"Your project's database migration `{up_rel}` has no matching rollback script `{down_rel}` next to "
        f"it. A migration changes your database's structure; a paired rollback script lets a migration that "
        f"goes wrong be undone cleanly, while without one reversing it is manual and risky. If this migration "
        f"is deliberately one-way that can be a fine choice — this is a gentle hygiene nudge, not a blocker. "
        f"Nothing is stopped, and this check never changes a file; to clear it, add the `{down_rel}` rollback "
        f"script."
    )


def _flyway_missing_message(v_rel: str, token: str) -> str:
    return (
        f"Your project's Flyway migration `{v_rel}` has no matching undo script (a `U{token}__….sql` file), "
        f"even though this project uses Flyway undo scripts elsewhere — so this one can't be rolled back the "
        f"way the others can. A paired undo script lets a change that goes wrong be reversed cleanly. This is "
        f"a gentle hygiene nudge, not a blocker: nothing is stopped, and this check never changes a file; to "
        f"clear it, add the matching undo script."
    )


def _forward_only_message(frameworks: list) -> str:
    names = _join(frameworks)
    return (
        f"Your project's {names} migrations (the scripts that change your database's structure) are "
        f"forward-only — they run in one direction by design, so there's no separate rollback file for this "
        f"check to look for. That's a normal setup, not a problem. This is a soft note only: it never blocks "
        f"a merge and never changes a file."
    )


def _in_file_message(frameworks: list) -> str:
    names = _join(frameworks)
    return (
        f"Your project's {names} migrations (the scripts that change your database's structure) keep each "
        f"rollback inside the migration file itself, so {names} handles reversing them — there's no separate "
        f"rollback file for this check to add. This is a soft note only: it never blocks a merge and never "
        f"changes a file."
    )


_GENERIC_DETECTED_MESSAGE = (
    "This check found a migrations folder in your project but couldn't match it to a migration tool it "
    "recognizes, and found no separate up/down rollback scripts (the `*.up.sql` paired with `*.down.sql` "
    "convention) to check. If your migrations use separate rollback files, adding the missing ones lets a "
    "change be undone cleanly; if your tool is forward-only or keeps rollbacks inside each migration, there's "
    "nothing to do here. This is a soft note only: it never blocks a merge and never changes a file."
)

_NO_OP_MESSAGE = (
    "Migration rollback checking isn't active here yet — this check looks for database migrations in your "
    "project (for example a `migrations/` folder, `db/migrate/`, `supabase/migrations/`, or "
    "`prisma/migrations/`) and didn't find any. That's a normal, expected state for a project that hasn't "
    "added database migrations yet, not an error: it starts looking for missing rollback scripts on its own "
    "once your project adds migrations."
)


def findings(tier: str, root: "str | None" = None) -> list:
    """The rollback-presence findings for `root` (defaults to `validate.ROOT`), as a list of finding.v1 dicts.

    Empty list = a genuine clean pass (every separate-file migration has its rollback). One `soft` nudge per
    unpaired separate-file migration. A single calm `soft` finding for each not-applicable state
    (forward-only / in-file / migrations-detected-but-unclassified / no-migrations) — said plainly, never a
    silent pass. Every finding carries `tier` severity (`soft`) — never `hard`."""
    root = root or validate.ROOT
    info = _scan(root)

    # (1) The separate up/down rollback convention — the live nudge. Presence-first: if any *.up.sql exists,
    #     resolve pairing regardless of which framework is also detected (hand-written rollbacks win).
    if info["up"]:
        missing = [rel for rel in sorted(info["up"])
                   if _expected_down(rel).lower() not in info["down"]]
        return [validate.finding(tier, _missing_rollback_message(rel, _expected_down(rel)),
                                 {"file": rel, "line": None}) for rel in missing]

    # (2) Flyway, paired PER VERSION whenever the project uses undo migrations at all — so one undo file can
    #     never mask a later versioned migration that has none (the silent-pass the single-boolean shortcut
    #     would allow). All paired -> empty list -> a genuine clean pass.
    if info["flyway_u"]:
        missing = [(rel, tok) for rel, tok in sorted(info["flyway_v"]) if tok not in info["flyway_u"]]
        return [validate.finding(tier, _flyway_missing_message(rel, tok),
                                 {"file": rel, "line": None}) for rel, tok in missing]

    # (3) Forward-only / no-rollback-concept frameworks, only when no separate rollback artifact was found.
    forward_only = _forward_only_frameworks(root, info)
    if forward_only:
        return [validate.disclosed_noop(_forward_only_message(forward_only), None)]

    # (4) Frameworks that keep the rollback inside each migration file.
    in_file = _in_file_frameworks(root, info)
    if in_file:
        return [validate.disclosed_noop(_in_file_message(in_file), None)]

    # (5) A migrations directory exists but matched no known tool and carried no separate rollback files.
    if info["migration_dir"]:
        return [validate.disclosed_noop(_GENERIC_DETECTED_MESSAGE, None)]

    # (6) No migrations at all — a calm no-op said plainly, never a silent pass.
    return [validate.disclosed_noop(_NO_OP_MESSAGE, None)]


def emit_findings() -> int:
    """The no-argument path the validator invokes: print the finding.v1 array and return 0."""
    print(json.dumps(findings("soft")))
    return 0


def demo() -> int:
    """Prove the inspector nudges a migration missing its rollback (both the up/down and the Flyway
    per-version conventions), passes a fully-paired set, says plainly (never silently) what it found on each
    not-applicable state with the right framework named, and never counts a migration under a pruned/walled
    directory (`.engine/`, `node_modules/`) as the product's own (the engine/product wall) — RETURNS NON-ZERO if any
    invariant is broken (the falsification can fail). Mutation-free: every case runs against a throwaway temp
    root, so the real working tree is never touched."""
    import shutil
    import tempfile

    def _seed(files: dict) -> str:
        d = tempfile.mkdtemp(prefix="engine-rollback-demo-")
        for rel, body in files.items():
            path = os.path.join(d, rel)
            parent = os.path.dirname(path)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
        return d

    cases = []  # (label, seeded files, predicate over the findings list)
    cases.append(("an *.up.sql with no *.down.sql earns one soft rollback nudge naming the file",
                  {"migrations/0001_init.up.sql": "create table t();"},
                  lambda fs: len(fs) == 1 and fs[0]["severity"] == "soft"
                  and fs[0]["location"] == {"file": "migrations/0001_init.up.sql", "line": None}
                  and "no matching rollback script" in fs[0]["message"]))
    cases.append(("a paired up/down migration passes cleanly",
                  {"migrations/0001_init.up.sql": "create table t();",
                   "migrations/0001_init.down.sql": "drop table t;"},
                  lambda fs: fs == []))
    cases.append(("Flyway with an undo for every version passes cleanly",
                  {"db/migration/V1__a.sql": "x", "db/migration/U1__a.sql": "x"},
                  lambda fs: fs == []))
    cases.append(("Flyway that uses undo but leaves one version unpaired nudges that version (no silent pass)",
                  {"db/migration/V1__a.sql": "x", "db/migration/U1__a.sql": "x",
                   "db/migration/V2__b.sql": "x"},
                  lambda fs: len(fs) == 1 and fs[0]["location"]["file"] == "db/migration/V2__b.sql"
                  and "no matching undo script" in fs[0]["message"]))
    cases.append(("Flyway with versioned migrations but no undo at all says forward-only",
                  {"db/migration/V1__a.sql": "x"},
                  lambda fs: len(fs) == 1 and "Flyway" in fs[0]["message"]
                  and "forward-only" in fs[0]["message"]))
    cases.append(("Supabase (forward-only) says so plainly, never silently",
                  {"supabase/config.toml": "project_id='x'", "supabase/migrations/0001_x.sql": "select 1;"},
                  lambda fs: len(fs) == 1 and "forward-only" in fs[0]["message"]
                  and "Supabase" in fs[0]["message"] and "soft note only" in fs[0]["message"]))
    cases.append(("Prisma (forward-only) says so naming Prisma",
                  {"prisma/schema.prisma": "datasource db {}"},
                  lambda fs: len(fs) == 1 and "Prisma" in fs[0]["message"]
                  and "forward-only" in fs[0]["message"]))
    cases.append(("a forward-only tool with hand-written rollbacks is checked, not waved off (presence-first)",
                  {"supabase/config.toml": "project_id='x'",
                   "supabase/migrations/0001_x.up.sql": "create table t();"},
                  lambda fs: len(fs) == 1 and "no matching rollback script" in fs[0]["message"]))
    cases.append(("Rails (in-file rollback) says so naming Rails",
                  {"db/migrate/0001_create_users.rb": "class X; end"},
                  lambda fs: len(fs) == 1 and "Rails" in fs[0]["message"]
                  and "inside the migration file" in fs[0]["message"]))
    cases.append(("Django (in-file rollback) says so naming Django",
                  {"manage.py": "import django", "app/migrations/__init__.py": "",
                   "app/migrations/0001_initial.py": "class Migration: pass"},
                  lambda fs: len(fs) == 1 and "Django" in fs[0]["message"]))
    cases.append(("Alembic (in-file rollback) says so naming Alembic",
                  {"alembic.ini": "[alembic]"},
                  lambda fs: len(fs) == 1 and "Alembic" in fs[0]["message"]))
    cases.append(("an empty project says the no-op plainly (never a silent pass)",
                  {},
                  lambda fs: len(fs) == 1 and "isn't active here yet" in fs[0]["message"]))
    cases.append(("a migration under .engine/ is walled out (the engine/product wall) -> no-op, not a nudge",
                  {".engine/x/migrations/0001.up.sql": "create table t();"},
                  lambda fs: len(fs) == 1 and "isn't active here yet" in fs[0]["message"]))
    cases.append(("a migration under node_modules/ is pruned -> no-op, not a nudge",
                  {"node_modules/dep/migrations/0001.up.sql": "create table t();"},
                  lambda fs: len(fs) == 1 and "isn't active here yet" in fs[0]["message"]))

    failures = []
    for label, files, ok in cases:
        root = _seed(files)
        try:
            result = findings("soft", root=root)
        finally:
            shutil.rmtree(root, ignore_errors=True)
        if any(f.get("severity") == "hard" for f in result):
            failures.append(f"{label}: a rollback finding must never be hard, got {result}")
        elif not ok(result):
            failures.append(f"{label}: invariant broken, got {result}")

    if failures:
        print("DEMO FAILED — the rollback inspector broke an invariant:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("DEMO PASSED — the rollback inspector nudges a migration missing its rollback (up/down and Flyway "
          "per-version), passes a fully-paired set, says plainly (never silently) what it found on each "
          "not-applicable state with the right framework named, and never counts a migration under a pruned "
          "or engine-walled directory as the product's own.")
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return demo()
    return emit_findings()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
