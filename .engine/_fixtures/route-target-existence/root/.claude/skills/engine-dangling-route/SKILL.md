---
name: engine-dangling-route
description: A negative fixture route whose active target points at an operation that is not there.
invocation: model-only
user-invocable: false
engine-targets:
  - kind: operation
    ref: .engine/operations/this-operation-does-not-exist.md
    availability: active
---

## Steps

1. A negative fixture — its active operation target is missing, so the target-existence check bites.
