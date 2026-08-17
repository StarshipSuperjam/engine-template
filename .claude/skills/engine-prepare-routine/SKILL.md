---
name: engine-prepare-routine
description: Give guidance on setting up unattended, scheduled Engine work — without starting a run.
invocation: model-only
user-invocable: false
engine-targets:
  - kind: skill
    ref: engine-routine
    availability: active
---

## Steps

1. Give the operator guidance on configuring unattended, scheduled work and point them to the `engine-routine` command to set it up. This route only advises — it never enters a Routine run itself.
