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

1. Resolve and show the target repository first, then help the operator compose and file a well-formed Engine Issue through the Engine's issue helper (`.engine/tools/issue_author.py`), which applies the `engine` label by construction. Preview the structured request. File when the operator has authorized it; do not ask again for an already authorized submission.

2. Use the helper's complete create operation, not a rendered body passed to `gh issue create`. Existing `engine-issue-input.v1` requests remain accepted; the explicit `issue-submission-input.v1` envelope selects `engine` or `product`. Product scope preserves ordinary fields and refuses the Engine label.
3. New Engine creation requires explicit journal activation; read `.engine/operations/issue-recovery.md`. Missing credentials or activation is an actionable refusal. Never recover by bypassing the helper, changing the label or minting a new identity after an uncertain send.
