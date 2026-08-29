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
2. Discover the target rather than expecting the operator to know an internal id. If the `engine-memory` helper is live, call `list-withheld` with no content query. It returns identifiers, kinds, dates, and only the short recovery labels explicitly kept when items were withheld — never the withheld wording. Match the operator's description to those visible labels. If one result clearly matches, call `restore` with its record id. If none clearly matches or several do, show only that safe metadata and ask which one; unlabelled legacy entries remain selectable by date, kind, and id.
3. If the optional helper is off, use the committed-file fallback: run `uv run --directory .engine --frozen -- python tools/accepted_hook_dispatch.py attended --root .. --script .engine/tools/memory/forget.py --operation attended-list-withheld -- list-withheld`. Match and resolve exactly as above, then run the same accepted command with `--operation attended-restore-withheld -- restore-record <record-id>`. Never probe withheld content, reveal withheld wording, or leave restoration unavailable merely because live helpers are disabled.
