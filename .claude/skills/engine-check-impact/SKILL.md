---
name: engine-check-impact
description: Trace what a part connects to and what a change would affect before touching it.
invocation: model-only
user-invocable: false
engine-targets:
  - kind: operation
    ref: .engine/operations/knowledge-impact-check.md
    availability: active
---

## Steps

1. Enter and follow the procedure in `.engine/operations/knowledge-impact-check.md` to trace, for the part in question, what it is part of, what depends on it, and what governs it.
