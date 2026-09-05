#!/usr/bin/env python3
"""model_routing_check.py — the custom/script entry for engine/check/model-routing.

Three conditions, each a hard finding:

  1. `.engine/policies/model-routing-postures.json` loads as model-routing.v1. At boot a file that does not load
     silently drops the engine to its built-in conservative default — safe, but an operator edit that
     never took effect; the merge is where that is caught.
  2. The policy page's generated posture region matches what the data renders.
  3. The retired fenced form has not come back: no `<!-- posture:` marker in a policy page, and no code
     under .engine/tools that parses one (StarshipSuperjam/engine-template#821 deleted the parser).

`ENGINE_MODEL_ROUTING_PATH` (unset in production) lets the negative-fixture meta-check point THIS CHECK at a
seeded off-schema file so it is witnessed biting. The seam lives here, in the check, and is passed to the
loader as an explicit path: the boot-time loader itself reads no environment variable, because the lines it
loads are relayed to a session verbatim and must come only from the committed file.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402
import execution_environment as ee  # noqa: E402
import agent_coherence_check  # noqa: E402  (emit — the one finding.v1 writer)

ENV_OVERRIDE = "ENGINE_MODEL_ROUTING_PATH"
_FENCE_MARKER = "<!-- posture:"
_OWN = {"model_routing_check.py", "test_execution_environment.py"}


def revived_fence_forms(root: str | None = None) -> list:
    """(rel_path, line) for every policy page carrying a posture marker, and every tool (this check and its
    test excepted) whose source names the marker — the two halves of the retired form."""
    base = root or validate.ROOT
    hits = []
    for rel_dir, suffix in ((os.path.join(".engine", "policies"), ".md"), (os.path.join(".engine", "tools"), ".py")):
        top = os.path.join(base, rel_dir)
        for dirpath, _dirs, files in os.walk(top):
            for name in sorted(files):
                if not name.endswith(suffix) or name in _OWN:
                    continue
                path = os.path.join(dirpath, name)
                with open(path, encoding="utf-8", errors="replace") as fh:
                    for n, line in enumerate(fh, 1):
                        if _FENCE_MARKER in line:
                            hits.append((os.path.relpath(path, base), n))
    return hits


def findings(tier: str, root: str | None = None, routing_path: str | None = None) -> list:
    base = root or validate.ROOT
    out = []
    try:
        expected, actual = ee.posture_projection_status(base, routing_path)
    except ee.RoutingUnreadable as exc:
        out.append(validate.finding(tier, f"{ee._ROUTING_REL} does not load as model-routing.v1: {exc}. At boot the "
                                    f"engine would fall back to its built-in conservative default and the operator's "
                                    f"posture edit would never take effect; fix the file to match the schema.",
                                    validate.loc(os.path.join(base, ee._ROUTING_REL))))
        return out
    page = os.path.join(base, ee._POLICY_REL)
    if actual is None:
        out.append(validate.finding(tier, f"{ee._POLICY_REL} carries no generated posture region (the two marker "
                                    f"comments are missing or out of order); place the region once, then regenerate "
                                    f"it with `uv run --directory .engine --frozen -- python "
                                    f"tools/execution_environment.py render-postures`.", validate.loc(page)))
    elif actual != expected:
        out.append(validate.finding(tier, f"the generated posture region in {ee._POLICY_REL} has drifted from "
                                    f"{ee._ROUTING_REL}: regenerate it with `uv run --directory .engine --frozen -- "
                                    f"python tools/execution_environment.py render-postures` and commit the result.",
                                    validate.loc(page)))
    for rel, line in revived_fence_forms(base):
        out.append(validate.finding(tier, f"'{rel}' line {line} carries the retired `{_FENCE_MARKER}` marker form — the "
                                    f"posture lines live in {ee._ROUTING_REL} and nothing parses a fenced block any "
                                    f"more. Delete it.", validate.loc(os.path.join(base, rel), line)))
    return out


def main(argv: list) -> int:
    tier = os.environ.get("ENGINE_RULE_TIER", "hard")
    return agent_coherence_check.emit(findings(tier, routing_path=validate.env_override_path(ENV_OVERRIDE, None)))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
