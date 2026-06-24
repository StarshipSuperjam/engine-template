"""The migration-discipline module's read-only inspector tools.

Domain migration-governance detection that the module's check rules invoke — the rollback-presence inspector
(`rollback.py`) today. Strictly read-only: these tools inspect the *product's own* database-migration
artifacts and emit findings; they never run a product migration and never rewrite a migration or any product
file (the R5 mutation firewall). They walk the product tree with caches, dependency trees, and the engine's
own walled `.engine/` pruned out (the engine/product wall), and they read only file and directory NAMES and
presence — never SQL/DDL contents.
"""
