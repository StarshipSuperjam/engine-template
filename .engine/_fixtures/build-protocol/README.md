# `build-protocol/` — negative fixture for `engine/check/build-protocol`

`malformed-protocol.json` is a copy of the live protocol whose `review_consumers` names a roster the closed
schema does not admit. `target.json` points the check's loader at it (`ENGINE_BUILD_PROTOCOL_PATH`, the
input-substitution seam), and `expect.json` names the token of the intended finding: the protocol fails to
load as build-protocol.v1. The live file is never touched.
