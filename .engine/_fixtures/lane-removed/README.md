# `lane-removed/` — negative fixture for `engine/check/lane-removed`

`tree/.engine/tools/revived_promoter.py` is a seeded revival of the retired length-budget promotion lane: a tool
that imports the removed `audit_soft_promote` module. `target.json` points the check's scan root at this tree
(`ENGINE_LANE_REMOVED_ROOT`, the input-substitution seam) and `expect.json` names the intended finding's token.
The live tree is never touched.
