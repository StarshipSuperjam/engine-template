"""The dependency-discipline module's read-only inspector tools.

Domain dependency-governance detection that the module's check rules invoke — the pinning inspector
(`pinning.py`) today, the dependency-review-gate relay in a later slice. Strictly read-only: these tools
inspect the *product's own* dependency manifests and emit findings; they never rewrite a lockfile or any
product file (the R5 mutation firewall). They inspect the repository root only and never the engine's own
walled `.engine/` tooling (the engine/product wall).
"""
