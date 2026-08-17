---
name: engine-setup-design-review
description: turn on plan reviews before building
invocation: model-only
user-invocable: false
engine-targets:
  - kind: skill
    ref: engine-setup
    availability: active
---

## Steps

1. Check whether the `design-review` add-on is installed in this project.
2. If it is not installed, explain in plain language what it does and offer to add it through the normal setup step — never install it because this route matched; adding it is the operator's decision.
3. If it is installed, report the active capability and route the operator's request to the add-on's own workflow.
