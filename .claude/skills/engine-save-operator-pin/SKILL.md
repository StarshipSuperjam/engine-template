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

1. Only when the operator explicitly asks to remember something as a standing preference, save it as a pin with the memory pin tool (`.engine/tools/memory/pins.py`), then confirm it is saved and will carry across sessions. Never pin something inferred rather than asked for.
