#!/usr/bin/env python3
"""Operator-runnable demo: when does the engine say it "couldn't reach your project map"? (engine-template)

It answers, in plain words, a question a non-engineer can't read code to verify: *does my cold-boot dashboard
only warn about the project map when the map is genuinely unreachable — not on every clean session, and not when
the map was rebuilt on the fly?* It exercises the REAL logic end-to-end — `attention.rank_live` deciding whether
knowledge is degraded, the REAL `boot_slice` provenance (committed map vs. a live rebuild over the real
surfaces), and the REAL `boot.render_dashboard` operator copy. The ONLY thing faked is the deliberately
broken source in case 2 (a stand-in for "the map is genuinely gone"), so the demo is deterministic and needs
no network, no token, and no edits to your working tree.

Three cases, each rendered as you'd see it at boot:
  1) clean session, committed map present  -> NO "couldn't reach", NO rebuild heads-up   (the false alarm is gone)
  2) the map is genuinely unreachable       -> "I couldn't reach your project map" STILL fires (alarm preserved)
  3) committed map absent, rebuilt live      -> a DISTINCT "running on a rebuilt project map" heads-up, not an alarm

Run: uv run --directory .engine -- python tools/demo_map_reachability.py
"""
from __future__ import annotations
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import attention      # noqa: E402
import boot           # noqa: E402
import boot_slice     # noqa: E402
import knowledge_index   # noqa: E402

# A complete, healthy baseline of the signals boot.render_dashboard renders; each case overrides only the two
# fields this behaviour drives (att_degraded, map_rebuilt). Mirrors gather_signals' return shape.
_BASE = {"state": {"schema_version": 1, "standing_situation": {}, "integration_debt": {}},
         "refused": False, "gate": "on", "reason": None, "finding_count": 0, "register": "",
         "debt_count": 0, "debt_as_of": None, "att_lines": [], "att_degraded": [], "shipped": [],
         "stance": "Building", "strand": None, "pr_conflict": None, "restore_offer": None,
         "migration_revert": None, "audit_stale": None, "live_standing": None, "map_rebuilt": False}


class _Unreachable:
    """A stand-in for a source whose map is genuinely gone: find() raises (as knowledge_query.find does at rung
    4 — committed graph absent AND the live walk also fails). The one faked boundary in this demo."""
    WALK_EDGE_KINDS = knowledge_index.WALK_EDGE_KINDS
    EDGE_KINDS = knowledge_index.EDGE_KINDS

    def find(self):
        raise knowledge_index.KnowledgeUnavailable("the project map is gone (both rungs failed)")


def _knowledge_degraded(source) -> bool:
    """The REAL availability logic: rank live with NO focus (a clean session) over `source`, and report whether
    knowledge landed in degraded_inputs — i.e. whether boot would say it couldn't reach the map."""
    return "knowledge" in attention.rank_live(focus=None, source=source).get("degraded_inputs", [])


def _render(*, att_degraded, map_rebuilt) -> str:
    s = dict(_BASE)
    s.update(att_degraded=att_degraded, map_rebuilt=map_rebuilt)
    return boot.render_dashboard(s)


def _advisories(dash: str) -> list[str]:
    return [ln.strip("_ ") for ln in dash.splitlines() if ln.startswith("_I ") or "rebuilt project map" in ln]


def main(argv: list | None = None) -> int:
    print("When does the boot say it couldn't reach your project map? (the real availability logic + render)\n")
    failures: list[str] = []

    # 1) Clean session, committed map present. The real slice reads from the committed graph (from_live False),
    #    and rank_live with no focus must NOT degrade knowledge -> no "couldn't reach", no rebuild heads-up.
    committed = boot_slice.read(slice_path=os.path.join(tempfile.mkdtemp(), "slice.json"))
    deg1 = _knowledge_degraded(committed)
    dash1 = _render(att_degraded=(["knowledge"] if deg1 else []), map_rebuilt=bool(committed and committed.from_live))
    print("1) Clean session, committed map present:")
    for a in _advisories(dash1) or ["(no project-map advisory — correct)"]:
        print(f"   {a}")
    if deg1 or "couldn't reach your project map" in dash1 or "rebuilt project map" in dash1:
        failures.append("clean+committed should show NO project-map advisory")
    print()

    # 2) The map is genuinely unreachable (faked: find() raises). rank_live must degrade knowledge -> the real
    #    "I couldn't reach your project map" notice STILL fires. This is the safety case the fix preserves.
    deg2 = _knowledge_degraded(_Unreachable())
    dash2 = _render(att_degraded=(["knowledge"] if deg2 else []), map_rebuilt=False)
    print("2) The project map is genuinely unreachable:")
    for a in _advisories(dash2) or ["(no advisory — WRONG, the alarm should fire)"]:
        print(f"   {a}")
    if not deg2 or "couldn't reach your project map" not in dash2:
        failures.append("genuinely-unreachable should STILL fire 'couldn't reach your project map'")
    print()

    # 3) Committed graph.json absent -> a REAL live rebuild over the on-disk surfaces. The map is reachable (so
    #    NOT degraded), but the committed file is missing -> a DISTINCT heads-up, never the "couldn't reach" alarm.
    absent_graph = os.path.join(tempfile.mkdtemp(), "absent-graph.json")
    live = boot_slice.read(slice_path=os.path.join(tempfile.mkdtemp(), "slice.json"), graph_path=absent_graph)
    deg3 = _knowledge_degraded(live)
    dash3 = _render(att_degraded=(["knowledge"] if deg3 else []), map_rebuilt=bool(live and live.from_live))
    print("3) Committed map absent, rebuilt live:")
    for a in _advisories(dash3) or ["(no advisory)"]:
        print(f"   {a}")
    if not (live and live.from_live):
        failures.append("a graph-absent build should report from_live=True")
    if deg3 or "couldn't reach your project map" in dash3 or "rebuilt project map" not in dash3:
        failures.append("live-rebuild should show the rebuild heads-up and NOT 'couldn't reach'")
    print()

    print("The availability decision (rank_live), the slice provenance (committed vs. live rebuild over the real")
    print("surfaces), and the operator copy (render_dashboard) are all the engine's real logic. Only case 2's")
    print("broken source is faked. Nothing was written to your tree; no network or token was used.")

    if failures:
        print("\nDEMO FAILED:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
