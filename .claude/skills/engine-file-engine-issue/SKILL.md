---
name: engine-file-engine-issue
description: Help the operator file a well-formed Engine Issue for this project.
invocation: model-only
user-invocable: false
engine-targets:
  - kind: tool
    ref: .engine/tools/issue_author.py
    availability: active
---

## Steps

1. Resolve and show the target repository first, then help the operator compose and file a well-formed Engine Issue through the Engine's issue helper (`.engine/tools/issue_author.py`), which applies the `engine` label by construction. Confirm before filing.
