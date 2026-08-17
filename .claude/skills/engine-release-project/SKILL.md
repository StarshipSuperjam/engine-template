---
name: engine-release-project
description: Preview or carry out this project's established release procedure.
invocation: model-only
user-invocable: false
engine-targets:
  - kind: operation
    ref: .engine/operations/engine-release.md
    availability: active
  - kind: operation
    ref: .engine/operations/projects-release-advance.md
    availability: module-conditional
    owner: github-projects-sync
---

## Steps

1. Enter and follow the release procedure in `.engine/operations/engine-release.md`; when the github-projects-sync add-on is installed, its `projects-release-advance.md` extension advances the project view as part of the same procedure.
