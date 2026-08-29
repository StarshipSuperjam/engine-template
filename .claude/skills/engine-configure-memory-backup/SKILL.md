---
name: engine-configure-memory-backup
description: Set up or adjust backup of this project's Engine memory.
invocation: model-only
user-invocable: false
engine-targets:
  - kind: tool
    ref: .engine/tools/memory/backup_vault.py
    availability: active
---

## Steps

1. Help the operator set up or adjust backup of the Engine's memory using the memory backup tool (`.engine/tools/memory/backup_vault.py`), confirming the destination with them.
