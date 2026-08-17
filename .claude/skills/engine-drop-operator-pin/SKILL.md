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

1. Only when the operator explicitly asks, remove the named pin with the memory pin tool (`.engine/tools/memory/pins.py`); it stops being surfaced but stays recoverable.
