---
name: engine-show-parts
description: Show the generated map of what this Engine is made of — its parts, modules, and how they connect.
invocation: model-only
user-invocable: false
engine-targets:
  - kind: tool
    ref: .engine/tools/self_map.py
    availability: active
---

## Steps

1. Show the Engine's layout by running `uv run --directory .engine -- python tools/self_map.py show` and present the generated map as printed.
