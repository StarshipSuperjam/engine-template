---
name: engine-design-product
description: Enter product design intake, offering to install the product-design add-on when it is absent.
invocation: model-only
user-invocable: false
engine-targets:
  - kind: operation
    ref: .engine/operations/product-intake.md
    availability: module-conditional
    owner: product-design
---

## Steps

1. If the product-design add-on is installed, enter its intake procedure in `.engine/operations/product-intake.md`. If it is absent, explain the add-on and offer to add it through the normal setup step — never install it without the operator's consent.
