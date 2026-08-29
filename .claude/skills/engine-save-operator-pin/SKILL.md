---
name: engine-save-operator-pin
description: Save a standing preference or instruction the operator asks to remember across sessions.
invocation: model-only
user-invocable: false
engine-targets:
  - kind: tool
    ref: .engine/tools/memory/pins.py
    availability: active
---

## Steps

1. Only when the operator explicitly asks to remember something as a standing preference, save it through the accepted attended boundary: `uv run --directory .engine --frozen -- python tools/accepted_hook_dispatch.py attended --root .. --script .engine/tools/memory/pins.py --operation attended-pin-add -- add "<the operator's instruction>"`. Then confirm it is saved and will carry across sessions. Never pin something inferred rather than asked for.
