---
name: engine-drop-operator-pin
description: Remove a standing preference the operator previously asked to remember.
invocation: model-only
user-invocable: false
engine-targets:
  - kind: tool
    ref: .engine/tools/memory/pins.py
    availability: active
---

## Steps

1. Only when the operator explicitly asks, remove the named pin through the accepted attended boundary: `uv run --directory .engine --frozen -- python tools/accepted_hook_dispatch.py attended --root .. --script .engine/tools/memory/pins.py --operation attended-pin-remove -- remove <record-id>`. It stops being surfaced but stays recoverable.
