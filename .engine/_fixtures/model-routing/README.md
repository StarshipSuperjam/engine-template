# `model-routing/` — negative fixture for `engine/check/model-routing`

`malformed-routing.json` carries only the conservative posture, so it is off schema (the `qualified` posture is
required). `target.json` points the check at it (`ENGINE_MODEL_ROUTING_PATH`, read by the check and passed to the loader as an explicit path; the input-substitution
seam) and `expect.json` names the intended finding's token. The live file is never touched.
