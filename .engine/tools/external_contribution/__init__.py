"""The external-contribution module's read-only inspector tools.

Cross-repo external-contribution detection that the module's check rules invoke — the upstream-clean
inspector (`upstream_clean_check.py`) today. Strictly read-only: these tools inspect path lists (the
outgoing contribution diff and the engine-owned path set) and emit findings; they never rewrite a file
(the R5 mutation firewall).
"""
