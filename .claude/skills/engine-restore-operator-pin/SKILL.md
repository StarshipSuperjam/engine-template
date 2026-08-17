---
name: engine-restore-operator-pin
description: Bring back a standing preference the operator earlier removed.
invocation: model-only
user-invocable: false
engine-targets:
  - kind: tool
    ref: .engine/tools/memory/pins.py
    availability: active
---

## Steps

1. Only when the operator explicitly asks, restore a previously removed pin with the memory pin tool (`.engine/tools/memory/pins.py`).
