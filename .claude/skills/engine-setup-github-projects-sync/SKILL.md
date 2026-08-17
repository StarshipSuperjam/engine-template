---
name: engine-setup-github-projects-sync
description: set up a project board
invocation: model-only
user-invocable: false
engine-targets:
  - kind: skill
    ref: engine-setup
    availability: active
  - kind: operation
    ref: .engine/operations/projects-sync-setup.md
    availability: module-conditional
    owner: github-projects-sync
---

## Steps

1. Check whether the `github-projects-sync` add-on is installed in this project.
2. If it is not installed, explain in plain language what it does and offer to add it through the normal setup step — never install it because this route matched; adding it is the operator's decision.
3. If it is installed, enter its setup procedure in `.engine/operations/projects-sync-setup.md`.
