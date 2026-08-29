---
name: engine-restore-operator-pin
description: Bring back a standing preference the operator earlier removed.
invocation: model-only
user-invocable: false
engine-targets:
  - kind: tool
    ref: .engine/tools/memory/mcp_server.py
    availability: active
---

## Steps

1. Only when the operator explicitly asks, first run `uv run --directory .engine --frozen -- python tools/accepted_hook_dispatch.py ensure --root ..`.
2. Discover the target rather than expecting the operator to know an internal id. If the `engine-memory` helper is live, call `list-withheld`, match the operator's description to its returned identifiers, and call `restore` with the selected record id. If more than one result could match, show the safe metadata and ask which one.
3. If the optional helper is off, use the committed-file fallback: run `uv run --directory .engine --frozen -- python tools/accepted_hook_dispatch.py attended --root .. --script .engine/tools/memory/forget.py --operation attended-list-withheld -- list-withheld`, select the requested record id, then run the same accepted command with `--operation attended-restore-withheld -- restore-record <record-id>`. Never leave restoration unavailable merely because live helpers are disabled.
