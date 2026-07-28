"""The dependency-discipline module's read-only inspector tools.

Domain dependency-governance detection that the module's check rules invoke — the pinning inspector
(`pinning.py`) and the dependency-review-gate relay (`review.py`). Strictly read-only: these tools
inspect the *product's own* dependency manifests and emit findings; they never rewrite a lockfile or any
product file (the R5 mutation firewall). They inspect the repository root only and never the engine's own
walled `.engine/` tooling (the engine/product wall).
"""
