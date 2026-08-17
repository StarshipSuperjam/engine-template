---
name: engine-overlong-demo-route
description: This demonstration route deliberately carries a description that runs well past the one-hundred-and-twenty-character ceiling the route budget check enforces, so the guard bites on a real over-length description.
invocation: model-only
user-invocable: false
engine-targets:
  - kind: operation
    ref: .engine/operations/build-orchestration.md
    availability: active
---

## Steps

1. A negative fixture route — its description is over-length so leg A of the route-budget check fires.
