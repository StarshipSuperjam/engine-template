"""Startability + no-regression for validate.py's lazy third-party binding (core slice 27b-pre).

`validate.py` is `core`'s validation engine and the only engine module that imports third-party packages
(yaml, jsonschema). Those live in the uv-managed tool-runtime (.engine/.venv/), so validate.py binds them
LAZILY — a module-level PEP 562 `__getattr__` for `validate.<symbol>` consumers (e.g. wiring's ontology-entry
check and the schema-validation test helpers), plus a local import inside each function that uses them. This
makes `import validate` succeed on the Python standard library alone, BEFORE that runtime exists — which the
first-run setup tool requires, since it is the one tool that runs to bootstrap the runtime (D-156).

These tests prove (1) `import validate` and its path constants work with yaml+jsonschema forced absent, and
(2) when the packages ARE present the lazy symbols and the frontmatter/schema paths behave exactly as before.
"""
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import validate  # noqa: E402


# Block yaml+jsonschema via a sys.meta_path finder, then import validate on the stdlib alone. Run in a
# subprocess so the block is total (no warm cache) and deterministic on a machine that DOES carry the packages.
_IMPORT_SNIPPET = r"""
import sys
_BLOCK = {"yaml", "jsonschema"}
class _Blocker:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in _BLOCK:
            raise ImportError("startability test: '%s' is blocked" % name)
        return None
for _m in [n for n in list(sys.modules) if n.split(".")[0] in _BLOCK]:
    del sys.modules[_m]
sys.meta_path.insert(0, _Blocker())
try:                                  # the block must actually bite, or the test is vacuous
    import jsonschema
    print("BLOCKER-INEFFECTIVE"); sys.exit(3)
except ImportError:
    pass
import validate
assert validate.ROOT and validate.ENGINE_DIR, "path constants must resolve with the runtime deps absent"
print("VALIDATE-IMPORTABLE")
"""


class TestImportableWithoutRuntimeDeps(unittest.TestCase):
    def test_import_validate_without_yaml_or_jsonschema(self):
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        proc = subprocess.run([sys.executable, "-c", _IMPORT_SNIPPET],
                              cwd=HERE, env=env, capture_output=True, text=True)
        self.assertNotIn("BLOCKER-INEFFECTIVE", proc.stdout,
                         "the deps blocker stopped biting — this test would be vacuous")
        self.assertIn("VALIDATE-IMPORTABLE", proc.stdout,
                      f"`import validate` must succeed stdlib-only.\nstdout={proc.stdout!r}\nstderr={proc.stderr!r}")
        self.assertEqual(proc.returncode, 0, proc.stderr)


class TestLazySymbolsWhenPresent(unittest.TestCase):
    """With the packages present (this construction repo's runtime), the lazy binding must be invisible:
    every `validate.<symbol>` consumer and validate's own frontmatter/schema paths behave as a top-level
    import would. Guards against the regression the plan gate caught — a naive lazy move that deletes the
    public `validate.Draft202012Validator` / `validate.SchemaError` names breaks 16 consumers (incl. wiring)."""

    def test_module_level_third_party_symbols_resolve(self):
        self.assertEqual(validate.Draft202012Validator.__name__, "Draft202012Validator")
        self.assertEqual(validate.SchemaError.__name__, "SchemaError")
        self.assertTrue(hasattr(validate.yaml, "safe_load"), "validate.yaml resolves to the yaml module")

    def test_unknown_attribute_still_raises_attributeerror(self):
        with self.assertRaises(AttributeError):
            validate.no_such_symbol  # noqa: B018 — asserting the __getattr__ guard rejects unknown names

    def test_frontmatter_uses_the_lazy_yaml_path(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "doc.md")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("---\ntitle: hi\nkind: note\n---\nbody\n")
            self.assertEqual(validate.frontmatter(p), {"title": "hi", "kind": "note"})

    def test_load_suites_uses_the_lazy_jsonschema_path(self):
        # Exercises the internal Draft202012Validator use against the real committed suites.json.
        self.assertIsInstance(validate.load_suites(), dict)


if __name__ == "__main__":
    unittest.main()
