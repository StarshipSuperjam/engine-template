#!/usr/bin/env python3
"""Execution-environment awareness — observe the runtime doing the work, compare it against the operator's
committed qualification baseline (.engine/state/execution.json), and report a posture the engine uses to
orient ITSELF (not the operator, who already sees the runtime in their harness).

The split this module implements, mirroring standing_situation.py's live-vs-committed discipline but inverting
its refresh semantics:

  - The BASELINE (execution.json) is a FROZEN operator judgment: which environments are qualified, and a
    snapshot of the instruction-floor hashes / engine release / repo slug at the moment of qualification. It
    is written only by record_qualification() and becomes true only when the operator merges it. It is NEVER
    auto-refreshed — the whole point is to notice drift away from the frozen snapshot.
  - The OBSERVATION is derived LIVE and cheap each boot: the runtime (injected — providers.detect), the repo
    origin slug, the engine release, and the sha256 of the current instruction-floor files. Nothing about the
    observation is committed.
  - compare() yields one of four postures:
      matched      — qualified for THIS repo, every snapshot component verifiable and equal. The environment's
                     own posture guidance loads.
      changed      — qualified for this repo, every component verifiable, but one drifted. Conservative posture
                     + a re-qualify alarm.
      unqualified  — no qualification for this repo (genesis, a baseline qualified for a DIFFERENT repo, or one
                     whose live repo can't be resolved — a shipped/foreign or unverifiable baseline reads as
                     not-ours rather than as spurious drift). Conservative posture, calm.
      unknown      — the baseline could not be read at all. Conservative posture, stated plainly.

Two safety rules the postures enforce, both learned at the plan gate:
  1. A qualified entry with ANY unverifiable component (a null recorded hash, a live floor file that can't be
     read now, or a live repo slug that can't be resolved) NEVER resolves to matched — an un-checkable
     component is not a pass (it would silently disable drift detection or the repo scoping). It degrades to
     the conservative posture.
  2. record_qualification REFUSES to stamp qualified when a component is unobservable, so a qualified baseline
     never carries a null snapshot field in the first place.

This module is self-contained: it imports only the standard library and the stdlib-only `moment` time seam,
reads only committed files under the repo root, and takes the runtime as an injected value. It performs no writes except through record_qualification(),
which writes execution.json atomically and NEVER commits — the operator's merge is the qualification act.
"""
from __future__ import annotations
import hashlib
import json
import os
import subprocess
import sys

import moment  # the trailing-Z time seam; a stdlib-only leaf, so this module stays substantively self-contained
import repo_identity  # the single-homed, dependency-light origin-URL parser (StarshipSuperjam/engine-template#691)

# The genesis baseline — the single source of truth for the shape's zero value. instantiator seeds this and
# read_baseline() synthesizes it when the file is absent, so the two never drift out of one definition.
_GENESIS_ENVIRONMENT = {
    "status": "unqualified",
    "as_of": None,
    "repo": None,
    "engine_release": None,
    "floors": {},
    "model_alias": None,
    "evidence": None,
}


def genesis_baseline() -> dict:
    """A fresh genesis record (both environments unqualified). A new dict every call — callers may mutate it."""
    return {
        "schema_version": 1,
        "environments": {
            "claude": dict(_GENESIS_ENVIRONMENT),
            "codex": dict(_GENESIS_ENVIRONMENT),
        },
    }


ENVIRONMENTS = ("claude", "codex")
_BASELINE_REL = os.path.join(".engine", "state", "execution.json")
_POLICY_REL = os.path.join(".engine", "policies", "model-routing.md")          # the page; carries a generated projection
_ROUTING_REL = os.path.join(".engine", "policies", "model-routing-postures.json")        # the data the engine loads
_ROUTING_SCHEMA_REL = os.path.join(".engine", "schemas", "model-routing.v1.json")
#: The generated region's delimiters in model-routing.md — the projection of the JSON the engine actually
#: loads. Rendered by `execution_environment.py render-postures`; drift is a hard finding at merge.
POSTURE_REGION_BEGIN = "<!-- generated: model-routing postures (execution_environment.py render-postures; never hand-edit) -->"
POSTURE_REGION_END = "<!-- /generated: model-routing postures -->"

# The safe fallback posture — always available in code, so the engine has careful guidance even when the
# routing data is missing or unparseable. The operator tunes the posture lines in model-routing-postures.json;
# this constant is the floor beneath it, never a "future" placeholder.
_CONSERVATIVE_DEFAULT = [
    "Execution environment is not a verified qualified match here — run your full, careful ceremony.",
    "Make no model-dependent shortcuts; the running model's identity is not verified by the engine.",
]


class BaselineUnreadable(Exception):
    """Raised when execution.json exists but cannot be read or parsed. Never conflated with a MISSING file
    (which is benign — a repo that predates this feature has no baseline and sits, honestly, unqualified):
    a present-but-corrupt baseline is an unavailability, so the posture degrades to 'unknown' (conservative,
    stated plainly) rather than being read as genesis."""


class QualificationRefused(Exception):
    """Raised by record_qualification() when a component the qualified snapshot must freeze cannot be observed
    (the repo origin, the engine release, or an instruction-floor file). Refusing to stamp qualified with a
    null snapshot field is what keeps a qualified baseline from silently disabling its own drift detection."""


def _repo_root() -> str:
    """The repository root — the directory holding CLAUDE.md, AGENTS.md and .engine/ — three levels up from
    this file (.engine/tools/execution_environment.py)."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _sha256_file(path: str) -> str | None:
    """'sha256:' + hex over the raw bytes of a file, or None when it cannot be read (absence or an unreadable
    file both read as None — 'unverifiable', never a false hash)."""
    try:
        with open(path, "rb") as fh:
            return "sha256:" + hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def floor_paths(root: str) -> list[str]:
    """The instruction-floor files that steer the assistant, as repo-relative posix keys, in a stable order:
    CLAUDE.md and AGENTS.md at the root, then every .engine/conduct/*.md in sorted order. Only files that
    EXIST are listed — a floor present at qualification but gone now is absent here, which compare() reads as
    drift; the conduct set is walked live (not a fixed list) so an operator-added conduct code is tracked."""
    paths = []
    for name in ("CLAUDE.md", "AGENTS.md"):
        if os.path.isfile(os.path.join(root, name)):
            paths.append(name)
    conduct_dir = os.path.join(root, ".engine", "conduct")
    if os.path.isdir(conduct_dir):
        for fn in sorted(os.listdir(conduct_dir)):
            if fn.endswith(".md") and os.path.isfile(os.path.join(conduct_dir, fn)):
                paths.append(f".engine/conduct/{fn}")
    return paths


def _engine_release(root: str) -> str | None:
    """The engine release string from .engine/engine.json, or None when it cannot be read."""
    try:
        with open(os.path.join(root, ".engine", "engine.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        release = data.get("engine_release")
        return release if isinstance(release, str) and release else None
    except (OSError, ValueError):
        return None


# Host-anchored (^...) so a look-alike host (notgithub.com/owner/repo) can never match as a substring — the
def current_repo(root: str) -> str | None:
    """The repository's git-origin slug (owner/name), read locally, or None on any failure. Used to scope a
    qualification to the repo it was made for; parsed from the origin URL so it needs no network. The parse is
    single-homed in repo_identity (StarshipSuperjam/engine-template#691) — same host-anchored, homograph-safe
    discipline boot's repo_slug uses, because a mis-parsed slug would scope a qualification to the wrong repo."""
    try:
        out = subprocess.run(
            ["git", "-C", root, "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return repo_identity.parse_github_slug(out.stdout)


def observe(*, provider: str, repo: str | None, root: str | None = None) -> dict:
    """The live environment, from injected runtime + repo and the committed files under root. No model identity
    (the running model is not reliably observable at session start and never drives drift); no writes."""
    root = root or _repo_root()
    floors = {rel: _sha256_file(os.path.join(root, *rel.split("/"))) for rel in floor_paths(root)}
    return {
        "runtime": provider,
        "repo": repo,
        "engine_release": _engine_release(root),
        "floors": floors,
    }


def read_baseline(root: str | None = None) -> dict:
    """The committed baseline. A MISSING file returns a fresh genesis record (benign — unqualified). A present
    file that will not parse raises BaselineUnreadable (unavailability, never read as genesis)."""
    root = root or _repo_root()
    path = os.path.join(root, _BASELINE_REL)
    if not os.path.exists(path):
        return genesis_baseline()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise BaselineUnreadable(f"execution.json could not be read: {exc}") from exc
    if not isinstance(data, dict):
        raise BaselineUnreadable("execution.json is not a JSON object")
    return data


def compare(observed: dict, baseline: dict) -> dict:
    """The posture of the observed environment against the committed baseline — one of matched / changed /
    unqualified. (unknown comes only from a BaselineUnreadable upstream, handled in derive.) Returns
    {runtime, posture, drift}: drift is the list of changed components, populated only for 'changed'."""
    env = observed["runtime"]
    entry = (baseline.get("environments") or {}).get(env) or {}
    if entry.get("status") != "qualified":
        return {"runtime": env, "posture": "unqualified", "drift": []}
    # A qualification counts only in the repo it was made for. If the live repo can't be resolved, the repo
    # component is unverifiable — and Rule 1 says an un-checkable component never resolves to matched (a
    # foreign baseline whose floor hashes happen to match must not slip through), so degrade to conservative.
    # A resolved-but-different repo is a foreign/home-shipped baseline: calm (unqualified), never drift.
    if entry.get("repo"):
        if observed.get("repo") is None or entry["repo"] != observed["repo"]:
            return {"runtime": env, "posture": "unqualified", "drift": []}

    drift: list[str] = []
    unverifiable = False

    base_release = entry.get("engine_release")
    live_release = observed.get("engine_release")
    if base_release is None or live_release is None:
        unverifiable = True
    elif base_release != live_release:
        drift.append("engine release")

    base_floors = entry.get("floors") or {}
    live_floors = observed.get("floors") or {}
    for key in sorted(set(base_floors) | set(live_floors)):
        in_base, in_live = key in base_floors, key in live_floors
        base_hash, live_hash = base_floors.get(key), live_floors.get(key)
        if (in_base and base_hash is None) or (in_live and live_hash is None):
            unverifiable = True            # a recorded-null or a live-unreadable floor — cannot be checked
        elif not in_base or not in_live:
            drift.append(key)              # a floor file appeared or was removed since qualification
        elif base_hash != live_hash:
            drift.append(key)              # a floor file's content changed

    # Rule 1: an un-checkable component never resolves to matched — degrade to the conservative posture.
    if unverifiable:
        return {"runtime": env, "posture": "unqualified", "drift": []}
    if drift:
        return {"runtime": env, "posture": "changed", "drift": drift}
    return {"runtime": env, "posture": "matched", "drift": []}


class RoutingUnreadable(Exception):
    """Raised by load_routing_strict when model-routing-postures.json is missing, unparseable, or off schema. The boot
    path never sees it — load_routing swallows it into None — the merge check reports it."""


def _routing_path(root: str, path: str | None = None) -> str:
    """The data file to read: the committed one under `root`, or an explicit `path` a CHECK passes for a seeded
    fixture. Deliberately no environment seam: this loader feeds the lines a session relays to itself at boot,
    and text that reaches a session that way must come only from the committed file."""
    if path:
        return path if os.path.isabs(path) else os.path.join(root, path)
    return os.path.join(root, _ROUTING_REL)


def load_routing_strict(root: str | None = None, path: str | None = None) -> dict:
    """model-routing-postures.json (or an explicit `path`, for a check's seeded fixture) validated against
    model-routing.v1, or RoutingUnreadable naming the miss."""
    root = root or _repo_root()
    path = _routing_path(root, path)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError as exc:
        raise RoutingUnreadable(f"{_ROUTING_REL} is missing ({path})") from exc
    except (OSError, ValueError) as exc:
        raise RoutingUnreadable(f"{_ROUTING_REL} is not readable JSON: {exc}") from exc
    with open(os.path.join(root, _ROUTING_SCHEMA_REL), encoding="utf-8") as fh:
        schema = json.load(fh)
    from jsonschema import Draft202012Validator  # lazy: the tool-runtime dependency validate.py also defers
    errors = sorted(Draft202012Validator(schema).iter_errors(data), key=lambda e: list(e.absolute_path))
    if errors:
        where = "/".join(str(p) for p in errors[0].absolute_path) or "(top level)"
        raise RoutingUnreadable(f"{_ROUTING_REL} does not match model-routing.v1 at {where}: {errors[0].message}")
    return data


def load_routing(root: str | None = None) -> dict | None:
    """The routing data, or None on ANY miss — missing file, bad JSON, off schema, or an unexpected error.
    This is the boot-facing loader: it never raises, so resolve_posture always has the constant to fall
    back on. The merge check uses load_routing_strict to say WHY a file did not load."""
    try:
        return load_routing_strict(root)
    except Exception:
        return None


def resolve_posture(posture: str, root: str | None = None) -> list[str]:
    """The self-instruction lines the engine loads for a posture. A 'matched' environment loads the
    operator-authored qualified lines from model-routing-postures.json (or the safe constant if the data is absent
    or malformed); every other posture loads the conservative default. Never raises."""
    root = root or _repo_root()
    key = "qualified" if posture == "matched" else "conservative-default"
    routing = load_routing(root)
    lines = (routing or {}).get("postures", {}).get(key) if routing else None
    return list(lines) if lines else list(_CONSERVATIVE_DEFAULT)


def render_postures(routing: dict) -> str:
    """The generated region of model-routing.md, markers included: each posture's lines as the fenced text
    the operator reads, in the order the engine prefers them."""
    out = [POSTURE_REGION_BEGIN,
           f"The posture lines the engine loads, from `{_ROUTING_REL}`:", ""]
    for key, when in (("qualified", "loaded only for a `matched` environment"),
                      ("conservative-default", "loaded for every other posture, and the built-in floor when the data is absent")):
        out.append(f"**{key}** — {when}:")
        out.append("```text")
        out.extend(routing["postures"][key])
        out.append("```")
        out.append("")
    out.append(POSTURE_REGION_END)
    return "\n".join(out)


def _split_policy(text: str) -> tuple:
    """(before, region, after); region is None when the markers are absent, malformed, or duplicated (a second
    pair would be a place to hide prose behind a name this check guards)."""
    b = text.find(POSTURE_REGION_BEGIN)
    e = text.find(POSTURE_REGION_END)
    if b < 0 or e < 0 or e < b or text.count(POSTURE_REGION_BEGIN) > 1 or text.count(POSTURE_REGION_END) > 1:
        return text, None, ""
    end = e + len(POSTURE_REGION_END)
    return text[:b], text[b:end], text[end:]


def posture_projection_status(root: str | None = None, routing_path: str | None = None) -> tuple:
    """(expected_region, actual_region_or_None) for model-routing.md on disk; raises RoutingUnreadable when
    the data itself does not load (there is nothing to project). `routing_path` is the check's fixture seam."""
    root = root or _repo_root()
    expected = render_postures(load_routing_strict(root, routing_path))
    with open(os.path.join(root, _POLICY_REL), encoding="utf-8") as fh:
        _, actual, _ = _split_policy(fh.read())
    return expected, actual


def apply_posture_projection(root: str | None = None) -> bool:
    """Write the current projection into the page's generated region; True when the file changed. Refuses
    (RoutingUnreadable) when the page carries no region — a region is placed by hand once, then only
    regenerated."""
    root = root or _repo_root()
    path = os.path.join(root, _POLICY_REL)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    before, region, after = _split_policy(text)
    if region is None:
        raise RoutingUnreadable(f"{_POLICY_REL} carries no generated posture region to render into")
    new = before + render_postures(load_routing_strict(root)) + after
    if new == text:
        return False
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new)
    return True


def derive(*, provider: str, repo: str | None = None, root: str | None = None) -> dict:
    """The total, boot-safe entry point: observe + read the baseline + compare + resolve the posture lines,
    never raising. A missing baseline yields 'unqualified'; an unreadable one yields 'unknown'; any other
    failure also yields 'unknown' (conservative). The tool owns the posture decision AND its text; boot only
    relays. The returned dict carries {runtime, posture, drift, lines}."""
    root = root or _repo_root()
    try:
        if repo is None:
            repo = current_repo(root)
        observed = observe(provider=provider, repo=repo, root=root)
        result = compare(observed, read_baseline(root))
    except BaselineUnreadable:
        result = {"runtime": provider, "posture": "unknown", "drift": []}
    except Exception:
        result = {"runtime": provider, "posture": "unknown", "drift": []}
    try:
        result["lines"] = resolve_posture(result["posture"], root)
    except Exception:
        result["lines"] = list(_CONSERVATIVE_DEFAULT)
    return result


def _utcnow() -> str:
    return moment.utc_now()


def _write_atomic(root: str, data: dict) -> None:
    """Write execution.json as pretty JSON + trailing newline, atomically (temp + os.replace) so a crash never
    leaves a half-written baseline."""
    path = os.path.join(root, _BASELINE_REL)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def record_qualification(env: str, *, root: str | None = None, repo: str | None = None,
                         model_alias: str | None = None, evidence: str | None = None,
                         now: str | None = None) -> dict:
    """THE sole writer of execution.json. Stamps environment `env` as qualified with the LIVE-observed repo,
    engine release, and instruction-floor hashes, plus the operator-supplied model_alias/evidence. REFUSES
    (QualificationRefused) when the repo, the engine release, or any floor file cannot be observed — a
    qualified snapshot must never carry a null component. Writes the file only; it NEVER commits, because the
    operator's merge of the resulting diff IS the qualification act. `repo` defaults to the live git origin."""
    root = root or _repo_root()
    if env not in ENVIRONMENTS:
        raise ValueError(f"unknown environment {env!r}; expected one of {ENVIRONMENTS}")
    observed = observe(provider=env, repo=repo if repo is not None else current_repo(root), root=root)
    if observed["repo"] is None:
        raise QualificationRefused("the repository's git origin could not be determined")
    if observed["engine_release"] is None:
        raise QualificationRefused("the engine release could not be read from .engine/engine.json")
    floors = observed["floors"]
    if not floors or any(v is None for v in floors.values()):
        raise QualificationRefused(
            "an instruction-floor file could not be read; refusing to stamp qualified with an unverifiable floor")

    baseline = read_baseline(root)          # raises BaselineUnreadable rather than clobber a corrupt file
    entry = {
        "status": "qualified",
        "as_of": now or _utcnow(),
        "repo": observed["repo"],
        "engine_release": observed["engine_release"],
        "floors": floors,
        "model_alias": model_alias,
        "evidence": evidence,
    }
    baseline.setdefault("environments", {})[env] = entry
    _write_atomic(root, baseline)
    return entry


def main(argv: list | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if argv and argv[0] == "derive":
        env = argv[1] if len(argv) > 1 else "claude"
        print(json.dumps(derive(provider=env), indent=2))
        return 0
    if argv and argv[0] == "render-postures":
        changed = apply_posture_projection()
        print(f"{'rendered' if changed else 'already current'}: {_POLICY_REL}")
        return 0
    if argv and argv[0] == "check-postures":
        expected, actual = posture_projection_status()
        if actual == expected:
            print(f"current: {_POLICY_REL} matches {_ROUTING_REL}")
            return 0
        print(f"DRIFT: the generated posture region in {_POLICY_REL} does not match {_ROUTING_REL} — run "
              f"`execution_environment.py render-postures` and commit", file=sys.stderr)
        return 1
    if argv and argv[0] == "record":
        if len(argv) < 2 or argv[1] not in ENVIRONMENTS:
            print(f"usage: execution_environment.py record <{'|'.join(ENVIRONMENTS)}> "
                  f"[--model-alias A] [--evidence URL]", file=sys.stderr)
            return 2
        env = argv[1]
        model_alias = evidence = None
        rest = argv[2:]
        for i, tok in enumerate(rest):
            if tok == "--model-alias" and i + 1 < len(rest):
                model_alias = rest[i + 1]
            elif tok == "--evidence" and i + 1 < len(rest):
                evidence = rest[i + 1]
        try:
            entry = record_qualification(env, model_alias=model_alias, evidence=evidence)
        except QualificationRefused as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 1
        print(f"recorded {env} as qualified (uncommitted — review and merge the diff to qualify):")
        print(json.dumps(entry, indent=2))
        return 0
    print(f"usage: execution_environment.py derive [{'|'.join(ENVIRONMENTS)}] | "
          f"record <{'|'.join(ENVIRONMENTS)}> [--model-alias A] [--evidence URL] | render-postures | check-postures")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
