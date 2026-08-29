---
name: engine-save-operator-pin
description: Save a standing preference or instruction the operator asks to remember across sessions.
invocation: model-only
user-invocable: false
engine-targets:
  - kind: tool
    ref: .engine/tools/memory/pins.py
    availability: active
---

## Steps

1. Only when the operator explicitly asks, first run `uv run --directory .engine --frozen -- python tools/accepted_hook_dispatch.py ensure --root ..` so the accepted boundary is present.
2. Encode the operator's exact UTF-8 instruction as canonical URL-safe Base64, then pass only that shell-safe token through the accepted boundary: `uv run --directory .engine --frozen -- python tools/accepted_hook_dispatch.py attended --root .. --script .engine/tools/memory/pins.py --operation attended-pin-add -- add-base64 <url-safe-base64>`. Never interpolate the raw instruction into a shell command. Confirm it is saved and will carry across sessions. Never pin something inferred rather than asked for.
