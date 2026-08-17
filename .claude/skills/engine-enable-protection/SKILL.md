---
name: engine-enable-protection
description: Turn on branch protection and the Engine's control-plane safeguards for this project.
invocation: model-only
user-invocable: false
engine-targets:
  - kind: operation
    ref: .engine/operations/control-plane-bootstrap.md
    availability: active
---

## Steps

1. Enter and follow `.engine/operations/control-plane-bootstrap.md` to turn on branch protection and the control-plane safeguards, confirming with the operator before changing settings.
