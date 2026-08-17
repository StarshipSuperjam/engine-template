---
name: engine-manage-addons
description: Add or remove an optional Engine add-on for this project.
invocation: model-only
user-invocable: false
engine-targets:
  - kind: operation
    ref: .engine/operations/module-add.md
    availability: active
  - kind: operation
    ref: .engine/operations/module-remove.md
    availability: active
---

## Steps

1. To add an add-on, enter `.engine/operations/module-add.md`; to remove one, enter `.engine/operations/module-remove.md`. Adding or removing is always the operator's decision — offer and confirm; never install or remove because a request merely mentioned it.
