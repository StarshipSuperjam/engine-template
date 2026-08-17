---
name: engine-coordinate-build
description: Recognize a request to build something and name the Engine's build procedure to run.
invocation: model-only
user-invocable: false
engine-targets:
  - kind: operation
    ref: .engine/operations/build-orchestration.md
    availability: active
---

## Steps

1. Recognize that the operator is asking to build or change something, and name the Engine's build procedure in `.engine/operations/build-orchestration.md` as the way to run it. This route only recognizes and points — it does not begin building or change the session's stance by itself; the operator starts building through the established Build authorities.
