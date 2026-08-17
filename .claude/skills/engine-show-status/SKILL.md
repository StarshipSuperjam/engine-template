---
name: engine-show-status
description: Report where the project stands — open work, any alarms, and the current stance.
invocation: model-only
user-invocable: false
engine-targets:
  - kind: tool
    ref: .engine/tools/engine_status.py
    availability: active
---

## Steps

1. Show the project's standing by running `uv run --directory .engine --frozen -- python tools/engine_status.py --session "${CLAUDE_CODE_SESSION_ID}"` and relay the result plainly, surfacing any alarm.
