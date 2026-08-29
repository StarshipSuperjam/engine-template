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
2. Discover the target rather than expecting the operator to know an internal id. If the `engine-memory` helper is live, call `list-withheld` with the operator's own description as `query`. The helper matches it privately against the resident withheld notes and returns only identifiers, kinds, and dates — never the withheld wording. If one result matches, call `restore` with its record id. If more than one result matches, show only that safe metadata and ask which one.
3. If the optional helper is off, use the committed-file fallback. Encode the operator's exact description as canonical URL-safe Base64, then run `uv run --directory .engine --frozen -- python tools/accepted_hook_dispatch.py attended --root .. --script .engine/tools/memory/forget.py --operation attended-list-withheld -- list-withheld --query-base64 <encoded-description>`. If one result matches, run the same accepted command with `--operation attended-restore-withheld -- restore-record <record-id>`; if several match, ask using only their safe metadata. Never interpolate raw operator wording into a shell command, reveal withheld wording, or leave restoration unavailable merely because live helpers are disabled.
