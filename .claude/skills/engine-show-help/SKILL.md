---
name: engine-show-help
description: Show the operator the Engine's own commands — what they can type and what each one does.
invocation: model-only
user-invocable: false
engine-targets:
  - kind: tool
    ref: .engine/tools/engine_help.py
    availability: active
---

## Steps

1. List the Engine's commands by running `uv run --directory .engine -- python tools/engine_help.py`, then show the operator the listing exactly as printed — the commands they can type and what each does. Do not reword it.
