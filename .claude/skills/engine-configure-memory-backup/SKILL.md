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

1. Show the backup tool's read-only disclosure and confirm the destination with the operator.
2. After confirmation, establish or verify the accepted boundary with `uv run --directory .engine --frozen -- python tools/accepted_hook_dispatch.py ensure --root ..`.
3. Run setup through the accepted attended boundary: `uv run --directory .engine --frozen -- python tools/accepted_hook_dispatch.py attended --root .. --script .engine/tools/memory/backup_vault.py --operation attended-backup-setup -- setup --scope <shared|per-project> --consent y`.
4. A requested foreground backup uses that same boundary with `--operation automatic-backup -- now`; never run the mutating `now` verb directly from candidate code.
5. A requested vault restore uses that same boundary with `--script .engine/tools/memory/restore_vault.py --operation attended-restore-now -- restore`; relay its overwrite/resurrection prompt and do not supply consent on the operator's behalf.
