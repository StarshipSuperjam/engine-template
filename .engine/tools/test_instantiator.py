"""Tests for the first-run setup orchestrator's GATHER + CONFIRM half (core slice 27a).

Verifies: the not-set-up signal keys off the manifest's presence (no new state); identity is derived
best-effort and degrades when it can't be read; the optional features group by discipline in the fixed
order (empty when the catalog is empty; an unrecognized category is kept, not dropped); the walkthrough is
plain-language and states the destructive-on-confirm outcome; CONFIRM writes the manifest with the required
spine always plus the kept optionals (an unkept optional omitted, its files NOT touched here — deletion is a
later phase); the catalog the demo plants conforms to the catalog schema; and the demo runs green.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import instantiator as inst  # noqa: E402
import validate  # noqa: E402

_FORBIDDEN = ("orchestrat", "coherence", "wiring", "wires", "manifest", "idempotent", "venv", "sync",
              "lockfile", "pyproject", "ruleset", "override", "custom/script", "provides", "invocation",
              "model-auto", "operator-typed", "model-only", "foundation")


def _module(root, mid, status, version="1.0.0"):
    d = os.path.join(root, ".engine", "modules", mid)
    os.makedirs(d, exist_ok=True)
    inst._write_json(os.path.join(d, "manifest.json"),
                     {"id": mid, "version": version, "status": status, "provides": {}, "depends": {}})


class TestIsProvisioned(unittest.TestCase):
    def test_absent_manifest_is_not_provisioned(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, ".engine"))
            self.assertFalse(inst.is_provisioned(d), "no manifest yet → setup should run")

    def test_present_manifest_is_provisioned(self):
        with tempfile.TemporaryDirectory() as d:
            inst._write_json(inst._engine_manifest_path(d),
                             {"engine_release": "1.0.0", "packages": {"core": "1.0.0"}, "identity": "solo"})
            self.assertTrue(inst.is_provisioned(d), "a written manifest → already set up")


class TestDeriveIdentity(unittest.TestCase):
    def test_derives_owner_and_name_from_slug(self):
        with mock.patch.object(inst.boot, "repo_slug", return_value="acme/widgets"):
            ident = inst.derive_identity()
        self.assertEqual((ident["owner"], ident["name"]), ("acme", "widgets"))
        self.assertEqual(ident["branch"], inst.boot.PROTECTED_BRANCH)

    def test_degrades_when_slug_unreadable(self):
        with mock.patch.object(inst.boot, "repo_slug", return_value=None):
            ident = inst.derive_identity()
        self.assertIsNone(ident["owner"])
        self.assertIsNone(ident["name"])


class TestSelectable(unittest.TestCase):
    def test_empty_catalog_groups_to_nothing(self):
        self.assertEqual(inst.selectable([]), {})

    def test_grouped_in_fixed_category_order(self):
        entries = [
            {"verb": "engine-vv", "description": "x", "category": "Verification & Validation"},
            {"verb": "engine-pm", "description": "x", "category": "Product Management"},
        ]
        self.assertEqual(list(inst.selectable(entries).keys()),
                         ["Product Management", "Verification & Validation"], "PM before V&V (fixed order)")

    def test_unrecognized_category_kept_last_not_dropped(self):
        entries = [{"verb": "engine-x", "description": "x", "category": "Made Up"},
                   {"verb": "engine-pm", "description": "y", "category": "Product Management"}]
        keys = list(inst.selectable(entries).keys())
        self.assertIn("Made Up", keys, "an unexpected category is kept, never silently dropped")
        self.assertEqual(keys[-1], "Made Up", "an unexpected category sorts after the recognized ones")


class TestPresentGather(unittest.TestCase):
    def _gather(self, catalog_path=None):
        with mock.patch.object(inst.boot, "repo_slug", return_value="acme/widgets"):
            return inst.present_gather(catalog_path=catalog_path)

    def test_empty_catalog_shows_the_no_addons_line_and_choices(self):
        out = self._gather(None)
        self.assertIn(inst._EMPTY_CATALOG_LINE, out)
        self.assertIn("who reviews changes here", out, "the identity choice is presented")
        self.assertIn("will be removed from this project", out, "the destructive-on-confirm outcome is stated")

    def test_present_catalog_lists_the_command(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            with open(p, "w", encoding="utf-8") as fh:
                json.dump([{"id": "x", "verb": "engine-x", "description": "Does x.",
                            "category": "Product Management"}], fh)
            out = self._gather(p)
            self.assertIn("engine-x — Does x.", out)
            self.assertIn("Product Management", out)

    def test_walkthrough_is_plain_language(self):
        out = self._gather(None)
        low = out.lower()
        for term in _FORBIDDEN:
            self.assertNotIn(term, low, f"plain-language law: '{term}' must not surface to the operator")


class TestConfirm(unittest.TestCase):
    def test_writes_required_plus_kept_omitting_unkept(self):
        with tempfile.TemporaryDirectory() as d:
            _module(d, "core", "required")
            _module(d, "validators-core", "required")
            _module(d, "extras", "optional")
            with inst._redirect_root(d):
                res = inst.confirm(["extras"], "team", engine_release="2.0.0")
            man = res["manifest"]
            self.assertEqual(man["identity"], "team")
            self.assertEqual(man["engine_release"], "2.0.0")
            self.assertEqual(sorted(man["packages"]), ["core", "extras", "validators-core"],
                             "required spine always, plus the kept optional")
            # The written file is on disk and matches.
            with open(res["path"], encoding="utf-8") as fh:
                self.assertEqual(json.load(fh), man)

    def test_unkept_optional_is_omitted_but_its_files_remain(self):
        with tempfile.TemporaryDirectory() as d:
            _module(d, "core", "required")
            _module(d, "extras", "optional")
            with inst._redirect_root(d):
                res = inst.confirm([], "solo", engine_release="2.0.0")
            self.assertEqual(sorted(res["manifest"]["packages"]), ["core"],
                             "an unkept optional is left out of the manifest")
            self.assertTrue(os.path.isdir(os.path.join(d, ".engine", "modules", "extras")),
                            "CONFIRM deletes nothing — removal is a later phase")

    def test_confirm_makes_the_repo_provisioned(self):
        with tempfile.TemporaryDirectory() as d:
            _module(d, "core", "required")
            self.assertFalse(inst.is_provisioned(d))
            with inst._redirect_root(d):
                inst.confirm([], "solo", engine_release="1.0.0")
            self.assertTrue(inst.is_provisioned(d), "after confirm the checkpoint exists")

    def test_rerun_keeps_the_existing_release_when_none_passed(self):
        # A re-run that passes no release reads the one the existing manifest recorded rather than resetting it.
        with tempfile.TemporaryDirectory() as d:
            _module(d, "core", "required")
            inst._write_json(inst._engine_manifest_path(d),
                             {"engine_release": "9.9.9", "packages": {"core": "1.0.0"}, "identity": "solo"})
            with inst._redirect_root(d):
                res = inst.confirm([], "team")  # no engine_release → keep the recorded one
            self.assertEqual(res["manifest"]["engine_release"], "9.9.9", "a re-run keeps the recorded release")

    def test_manifest_with_no_id_does_not_crash_confirm(self):
        # Defense-in-depth on the committing step: a malformed (id-less) manifest is skipped, never a crash.
        with tempfile.TemporaryDirectory() as d:
            _module(d, "core", "required")
            bad = os.path.join(d, ".engine", "modules", "bad")
            os.makedirs(bad)
            inst._write_json(os.path.join(bad, "manifest.json"), {"status": "required", "version": "1.0.0"})
            with inst._redirect_root(d):
                res = inst.confirm([], "solo", engine_release="1.0.0")
            self.assertEqual(sorted(res["manifest"]["packages"]), ["core"], "the id-less manifest is skipped")


class TestCatalogSchemaConformance(unittest.TestCase):
    def _schema(self):
        with open(os.path.join(validate.ENGINE_DIR, "schemas", "provisioning-catalog.v1.json"),
                  encoding="utf-8") as fh:
            return json.load(fh)

    def test_committed_catalog_and_a_good_entry_validate(self):
        import jsonschema
        schema = self._schema()
        jsonschema.validate([], schema)  # the empty catalog this repo ships
        jsonschema.validate([{"id": "x-mod", "verb": "engine-x", "description": "Does x.",
                              "category": "Product Management", "status": "optional"}], schema)

    def test_missing_required_field_is_rejected(self):
        import jsonschema
        schema = self._schema()
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate([{"id": "x", "verb": "engine-x"}], schema)  # no description/category


class TestCLI(unittest.TestCase):
    def _run(self, argv):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = inst.main(argv)
        return rc, buf.getvalue()

    def test_demo_runs_green(self):
        rc, out = self._run(["demo"])
        self.assertEqual(rc, 0)
        self.assertIn("naming it, not hiding it", out, "the honest-ceiling banner leads the demo")

    def test_show_short_circuits_when_already_set_up(self):
        with tempfile.TemporaryDirectory() as d:
            _module(d, "core", "required")
            inst._write_json(inst._engine_manifest_path(d),
                             {"engine_release": "1.0.0", "packages": {"core": "1.0.0"}, "identity": "solo"})
            with inst._redirect_root(d):
                rc, out = self._run(["show"])
            self.assertEqual(rc, 0)
            self.assertIn(inst._ALREADY_SET_UP, out, "a set-up project short-circuits, never re-offering setup")

    def test_show_presents_the_walkthrough_when_not_set_up(self):
        with tempfile.TemporaryDirectory() as d:
            _module(d, "core", "required")  # no engine.json → not provisioned
            with inst._redirect_root(d), mock.patch.object(inst.boot, "repo_slug", return_value="acme/widgets"):
                rc, out = self._run([])
            self.assertEqual(rc, 0)
            self.assertIn("who reviews changes here", out, "an unset project shows the gather walkthrough")


# The first-run setup tool is the ONE engine tool that must run BEFORE the tool-runtime it installs exists
# (D-156): it bootstraps uv, so it cannot presuppose the packages the runtime provides (yaml, jsonschema).
# This block runs in a subprocess with those two packages forced absent via a sys.meta_path finder — proving
# `import instantiator` and the show/demo CLI start on the Python standard library alone, deterministically
# (independent of whether THIS machine's Python happens to carry the packages — it does, so the block is
# mandatory, and the block-bites guard fails loudly if it ever stops working).
_STARTABILITY_SNIPPET = r"""
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
    import yaml
    print("BLOCKER-INEFFECTIVE"); sys.exit(3)
except ImportError:
    pass
import io, contextlib
import instantiator                   # the import that used to transitively require the runtime
# `apply-demo` exercises the WHOLE apply chain (module_manager / wiring / bootstrap / knowledge_gen) with
# every boundary faked, and `finish-demo` exercises the verify + retire close (check_coherence +
# knowledge_gen.generate, both JSON/walk-only) — proving the heavy apply path AND the lifecycle close also
# start on the standard library alone (the whole instantiator runs on the operator's system python).
for _argv in (["show"], ["demo"], ["apply-demo"], ["finish-demo"]):
    _buf = io.StringIO()
    with contextlib.redirect_stdout(_buf):
        _rc = instantiator.main(_argv)
    assert _rc == 0, (_argv, _rc, _buf.getvalue()[-400:])
print("STARTABLE-OK")
"""


class TestStartabilityWithoutRuntime(unittest.TestCase):
    def _run_blocked(self):
        here = os.path.dirname(os.path.abspath(__file__))
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}  # don't lean on a venv site path
        return subprocess.run([sys.executable, "-c", _STARTABILITY_SNIPPET],
                              cwd=here, env=env, capture_output=True, text=True)

    def test_import_and_cli_start_without_yaml_or_jsonschema(self):
        proc = self._run_blocked()
        self.assertNotIn("BLOCKER-INEFFECTIVE", proc.stdout,
                         "the deps blocker stopped biting — the startability test would be vacuous")
        self.assertIn("STARTABLE-OK", proc.stdout,
                      "the setup tool must import and run `show`/`demo` with yaml+jsonschema absent "
                      "(it bootstraps the runtime that provides them).\n"
                      f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_runbook_launch_command_string_runs(self):
        # The verbatim command the runbook/skill tell the operator to type must actually run (from repo root,
        # the skill's cwd) — not just the underlying main() call.
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        proc = subprocess.run([sys.executable, ".engine/tools/instantiator.py", "show"],
                              cwd=root, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # In this construction repo the manifest is present, so the verb short-circuits "already set up".
        self.assertIn(inst._ALREADY_SET_UP.split("\n")[0], proc.stdout, proc.stdout)


# Apply copy is held to a STRICTER plain-language list than the gather copy: the apply phase also names the
# tool-runtime and the control-plane, whose maintainer terms must never reach the operator.
_FORBIDDEN_APPLY = _FORBIDDEN + ("control-plane", "tool-runtime")


def _confirmed_fixture(tmp, handle="octocat", keep=None):
    """Build a generated-repo fixture and confirm it (write the checkpoint), returning under a redirected
    root. Caller is responsible for the surrounding `with inst._redirect_root(tmp)`."""
    inst.confirm(keep or [], "solo", engine_release="1.0.0", handle=handle)


def _fake_apply(tmp, **overrides):
    """Run apply with every external boundary faked (no network, no machine writes, no real uv): the happy
    defaults, overridable per-test."""
    base = dict(
        announce=lambda t: None,
        home_reader=lambda: {},                                  # no global preference → adopt plan
        uv_present=lambda: None,                                 # uv not yet present
        uv_installer=lambda: os.path.join(tmp, ".engine", ".uv", "uv"),
        uv_runner=lambda uv, groups: True,
        consent=lambda kind: True,
        control_transport=inst._approve_transport(),
        gh_refresh=lambda s: True,
        control_issues=inst._FakeIssues(),
        control_repo="you/your-project",                         # injected so the control-plane step is
        control_token="tok",                                     # deterministic — never the CI/ambient token
    )
    base.update(overrides)
    return inst.apply(**base)


class TestApplyOrchestrator(unittest.TestCase):
    def test_refuses_when_not_confirmed(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d):
                res = inst.apply(announce=lambda t: None)   # no manifest written
            self.assertTrue(res["refused"])
            self.assertEqual(res["reason"], "not-confirmed")
            self.assertEqual(res["steps"], [])

    def test_full_happy_path_runs_all_seven_steps(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d):
                _confirmed_fixture(d)
                res = _fake_apply(d)
            self.assertFalse(res["refused"]); self.assertFalse(res["halted"])
            names = [s["step"] for s in res["steps"]]
            self.assertEqual(names, ["remove-unselected", "codeowners", "plan-mode",
                                     "tool-runtime", "substrates", "wires", "control-plane"])

    def test_handle_falls_back_to_the_manifest_value(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d):
                _confirmed_fixture(d, handle="manifest-owner")
                res = _fake_apply(d, handle=None)   # not passed → read from the manifest
                co = inst.validate.read(os.path.join(d, ".github", "CODEOWNERS"))
            self.assertIn("@manifest-owner", co)


class TestApplyStep1DeleteUnselected(unittest.TestCase):
    def test_deletes_the_unkept_module_keeps_required(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)                       # core (required) + extras-demo (optional)
            with inst._redirect_root(d):
                _confirmed_fixture(d, keep=[])           # keep nothing optional → extras-demo deselected
                res = _fake_apply(d)
            step = res["steps"][0]
            self.assertEqual(step["deleted"], ["extras-demo"])
            self.assertFalse(os.path.isdir(os.path.join(d, ".engine", "modules", "extras-demo")))
            self.assertTrue(os.path.isdir(os.path.join(d, ".engine", "modules", "core")), "required spine kept")

    def test_reverse_dependency_refusal_is_recorded_not_crashed(self):
        # A kept module depends on an unkept one → remove() refuses; apply records it and continues (the kept
        # module and its dependency both remain — coherent — and the phase never crashes).
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            _module(d, "depend200", "optional")
            # 'core' (kept, required) depends on 'depend200' (not kept) → removing depend200 is refused.
            inst._write_json(os.path.join(d, ".engine", "modules", "core", "manifest.json"),
                             {"id": "core", "version": "1.0.0", "status": "required",
                              "provides": {}, "wires": [], "depends": {"depend200": "*"}})
            with inst._redirect_root(d):
                _confirmed_fixture(d, keep=[])           # neither extras-demo nor depend200 kept
                res = _fake_apply(d)
            step = res["steps"][0]
            refused_ids = {r["id"] for r in step["refused"]}
            self.assertIn("depend200", refused_ids, "a still-depended-on module is refused, not deleted")
            self.assertTrue(os.path.isdir(os.path.join(d, ".engine", "modules", "depend200")), "kept on refusal")
            self.assertFalse(res["halted"], "a refusal records-and-continues; it never halts the phase")


class TestApplyStep2Codeowners(unittest.TestCase):
    def test_writes_block_and_owns_itself(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d):
                _confirmed_fixture(d, handle="acme")
                res = _fake_apply(d)
                co = inst.validate.read(os.path.join(d, ".github", "CODEOWNERS"))
            self.assertEqual(res["steps"][1]["status"], "written")
            self.assertIn("/.github/CODEOWNERS @acme", co, "the block owns itself from the first render")
            self.assertIn("/.engine/engine.json @acme", co)

    def test_degrades_without_a_handle(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d):
                _confirmed_fixture(d, handle=None)       # confirm wrote no handle
                res = _fake_apply(d, handle=None)
            step = res["steps"][1]
            self.assertEqual(step["status"], "degraded")
            self.assertFalse(os.path.isfile(os.path.join(d, ".github", "CODEOWNERS")), "no render without a handle")
            self.assertFalse(res["halted"], "a missing handle degrades, never halts")


class TestApplyStep3PlanMode(unittest.TestCase):
    def _mode(self, tmp):
        return (inst._read_json_or(os.path.join(tmp, ".claude", "settings.json"), {})
                .get("permissions", {}).get("defaultMode"))

    def test_adopts_when_no_global_preference(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d):
                _confirmed_fixture(d)
                res = _fake_apply(d, home_reader=lambda: {})
            self.assertEqual(res["steps"][2]["status"], "adopted")
            self.assertEqual(self._mode(d), "plan")

    def test_conflict_keeps_operator_default_when_declined(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d):
                _confirmed_fixture(d)
                res = _fake_apply(d, home_reader=lambda: {"permissions": {"defaultMode": "acceptEdits"}},
                                  consent=lambda kind: False)        # operator declines adopt
            self.assertEqual(res["steps"][2]["status"], "kept-operator-default")
            self.assertIsNone(self._mode(d), "keep writes nothing — the project key stays unset (the yield)")

    def test_conflict_adopts_when_operator_chooses(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d):
                _confirmed_fixture(d)
                res = _fake_apply(d, home_reader=lambda: {"permissions": {"defaultMode": "acceptEdits"}},
                                  consent=lambda kind: kind == "plan-mode-adopt")
            self.assertEqual(res["steps"][2]["status"], "adopted")
            self.assertEqual(self._mode(d), "plan")

    def test_never_writes_home_settings(self):
        # The yield-to-the-operator law: ~/.claude is read-only. We assert by giving a home_reader that would
        # raise if WRITTEN (it is a pure value), and checking the project — but the strongest guard is that
        # apply has no path that writes the home file; the conflict path writes nothing at all.
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            calls = {"n": 0}
            def reader():
                calls["n"] += 1
                return {"permissions": {"defaultMode": "acceptEdits"}}
            with inst._redirect_root(d):
                _confirmed_fixture(d)
                _fake_apply(d, home_reader=reader, consent=lambda kind: False)
            self.assertGreaterEqual(calls["n"], 1, "the global default is READ")


class TestApplyStep4ToolRuntime(unittest.TestCase):
    def test_materializes_when_present_and_synced(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d):
                _confirmed_fixture(d)
                res = _fake_apply(d, uv_present=lambda: "/usr/bin/uv", uv_runner=lambda uv, g: True)
            self.assertEqual(res["steps"][3]["status"], "materialized")
            self.assertFalse(res["halted"])

    def test_halts_without_consent_to_install(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d):
                _confirmed_fixture(d)
                res = _fake_apply(d, uv_present=lambda: None, consent=lambda kind: False)
            self.assertTrue(res["halted"])
            self.assertEqual(res["steps"][-1]["step"], "tool-runtime")
            self.assertEqual(len(res["steps"]), 4, "steps 5-7 are not attempted on a halt")

    def test_halts_when_install_fails(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d):
                _confirmed_fixture(d)
                res = _fake_apply(d, uv_present=lambda: None, uv_installer=lambda: None)
            self.assertTrue(res["halted"])
            self.assertEqual(res["steps"][-1]["status"], "degraded")

    def test_halts_when_sync_fails_never_falls_back(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d):
                _confirmed_fixture(d)
                res = _fake_apply(d, uv_present=lambda: "/usr/bin/uv", uv_runner=lambda uv, g: False)
            self.assertTrue(res["halted"])
            names = [s["step"] for s in res["steps"]]
            self.assertNotIn("substrates", names, "no substrate init on a degraded runtime (never system python)")

    def test_install_uses_unmanaged_path_and_pinned_versioned_url(self):
        # The real installer command (faked everywhere else) must use the PATH-independent unmanaged install
        # and the version-pinned official URL — the deployed supply-chain contract (D-156).
        self.assertIn("UV_UNMANAGED_INSTALL", inst._install_uv.__doc__ or "")
        self.assertEqual(inst.UV_INSTALL_URL, f"https://astral.sh/uv/{inst.UV_PIN}/install.sh")
        self.assertEqual(inst.UV_PIN, "0.11.8", "must match the committed CI uv pin")


class TestApplyStep6WiresInstallsHooks(unittest.TestCase):
    def test_apply_installs_hooks_not_only_the_query_server(self):
        # B1: the apply phase must wire ALL of a kept module's wires — the HOOKS that boot/gate/close the
        # engine, not only the MCP server. A hook-less generated repo is otherwise an inert engine.
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)                      # ships a HOOK-LESS settings.json
            settings_before = inst._read_json_or(os.path.join(d, ".claude", "settings.json"), {})
            self.assertNotIn("hooks", settings_before, "the fixture models a published, un-wired template")
            with inst._redirect_root(d):
                _confirmed_fixture(d)
                _fake_apply(d)
                after = inst._read_json_or(os.path.join(d, ".claude", "settings.json"), {})
                mcp = inst._read_json_or(os.path.join(d, ".mcp.json"), {})
            self.assertIn("hooks", after, "apply must install the engine's hooks")
            for event in ("SessionStart", "PreToolUse", "Stop"):
                self.assertIn(event, after["hooks"], f"the {event} hook must be wired")
            self.assertIn("engine-knowledge-graph", mcp.get("mcpServers", {}), "and the query server too")


class TestApplyStep7ControlPlane(unittest.TestCase):
    def test_applied_turns_the_gate_on(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d):
                _confirmed_fixture(d)
                res = _fake_apply(d, control_transport=inst._approve_transport())
            cp = res["steps"][-1]
            self.assertEqual(cp["status"], "applied"); self.assertTrue(cp["protected"])

    def test_degraded_never_pretends_and_phase_ends(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d):
                _confirmed_fixture(d)
                res = _fake_apply(d, control_transport=inst._defer_transport(), gh_refresh=lambda s: False)
            cp = res["steps"][-1]
            self.assertEqual(cp["status"], "degraded"); self.assertFalse(cp["protected"])
            self.assertFalse(res["halted"], "a deferred gate ends the phase cleanly; it never halts")

    def test_degraded_when_no_repo_or_token(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d), \
                 mock.patch.object(inst.boot, "repo_slug", return_value=None), \
                 mock.patch.object(inst.boot, "gh_token", return_value=None):
                _confirmed_fixture(d)
                # opt out of the injected coordinates so the no-repo/no-sign-in path is exercised
                res = _fake_apply(d, control_repo=None, control_token=None)
            self.assertEqual(res["steps"][-1]["status"], "degraded")
            self.assertIn("no project", res["steps"][-1]["detail"])


class TestApplyIdempotentResume(unittest.TestCase):
    def test_rerun_no_ops_the_writing_steps(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d):
                _confirmed_fixture(d)
                _fake_apply(d, control_transport=inst._approve_transport())
                second = _fake_apply(d, control_transport=inst._already_transport())
            by = {s["step"]: s["status"] for s in second["steps"]}
            self.assertFalse(second["halted"])
            self.assertEqual(by["codeowners"], "already", "a resumed render is a true no-op")
            self.assertEqual(by["plan-mode"], "already")
            self.assertEqual(by["control-plane"], "already")


class TestApplyIsolation(unittest.TestCase):
    def test_apply_under_redirect_leaves_real_files_untouched(self):
        # The construction repo's own engine files must be byte-for-byte unchanged by a redirected apply —
        # the demo's mechanical isolation guarantee, as a test (catches a path constant escaping the fixture).
        snap = inst._snapshot_real_files()
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d):
                _confirmed_fixture(d)
                _fake_apply(d)
        self.assertTrue(inst._assert_real_files_unchanged(snap), "a redirected apply must not touch real files")


class TestDeriveHandle(unittest.TestCase):
    def test_returns_login_on_success(self):
        fake = mock.Mock(returncode=0, stdout="octocat\n")
        with mock.patch("subprocess.run", return_value=fake):
            self.assertEqual(inst.derive_handle(), "octocat")

    def test_returns_none_when_gh_absent_or_empty(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            self.assertIsNone(inst.derive_handle())
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=1, stdout="")):
            self.assertIsNone(inst.derive_handle())


class TestApplyCopySurface(unittest.TestCase):
    def test_template_carries_every_apply_section(self):
        copy = inst.load_copy(inst.TEMPLATE_PATH)
        for key in inst.COPY_HEADINGS:
            self.assertTrue(copy[key].strip(), f"apply copy section {key!r} missing from the template")

    def test_missing_template_falls_back_not_crashes(self):
        copy = inst.load_copy("/no/such/first-run.md")
        self.assertEqual(copy["tool-runtime-consent"], inst.FALLBACK_COPY["tool-runtime-consent"])

    def test_apply_copy_is_plain_language(self):
        # The expanded plain-language law over the apply copy surface AND the rendered control-plane banners
        # the apply phase surfaces (no tool-runtime / control-plane / venv / ruleset … reaches the operator).
        surfaces = list(inst.load_copy(inst.TEMPLATE_PATH).values())
        surfaces += [inst.bootstrap.render(inst.bootstrap.Result(s, "main", ["x"], c))
                     for s, c in (("applied", None), ("already", None), ("degraded", "not-admin"),
                                  ("degraded", "org-policy"), ("degraded", "didnt-save"))]
        blob = "\n".join(surfaces).lower()
        for term in _FORBIDDEN_APPLY:
            self.assertNotIn(term, blob, f"plain-language law: '{term}' must not surface in the apply copy")


class TestApplyChainRunsOnSystemPython39(unittest.TestCase):
    # The apply phase runs on the operator's SYSTEM python (3.9 on macOS) BEFORE it installs the 3.11+
    # runtime. An evaluated `X | None` annotation raises there; `from __future__ import annotations` defers
    # it. Hold every tool the instantiator's apply chain imports to that, so a future edit can't silently
    # re-break a real adopter's first run (bootstrap.py was the gap this slice closed).
    _APPLY_CHAIN = ("instantiator", "module_manager", "wiring", "bootstrap", "knowledge_gen",
                    "module_coherence", "boot", "telemetry", "protection_guard", "hooks",
                    "module_catalog", "validate")

    def test_every_apply_chain_tool_defers_annotations(self):
        here = os.path.dirname(os.path.abspath(__file__))
        missing = []
        for name in self._APPLY_CHAIN:
            with open(os.path.join(here, name + ".py"), encoding="utf-8") as fh:
                if "from __future__ import annotations" not in fh.read():
                    missing.append(name)
        self.assertEqual(missing, [], f"system-python-launched tools must defer annotations: {missing}")


class TestApplyDemoRunsGreen(unittest.TestCase):
    def test_apply_demo_exits_zero(self):
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = inst.main(["apply-demo"])
        out = buf.getvalue()
        self.assertEqual(rc, 0, out)
        self.assertIn("byte-for-byte unchanged", out, "the isolation guarantee is shown")
        self.assertIn("naming it, not hiding it", out, "the honest-ceiling banner leads the demo")


class TestApplyCli(unittest.TestCase):
    def test_apply_refuses_plainly_without_confirmation(self):
        import contextlib, io
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            buf = io.StringIO()
            with inst._redirect_root(d), contextlib.redirect_stdout(buf):
                rc = inst.main(["apply"])      # no manifest → refuse
            self.assertEqual(rc, 1)
            self.assertIn("hasn't been confirmed", buf.getvalue())

    def test_confirm_then_apply_chain(self):
        import contextlib, io
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d), contextlib.redirect_stdout(io.StringIO()):
                rc1 = inst.main(["confirm", "--tier", "solo", "--keep", "", "--handle", "octocat"])
            self.assertEqual(rc1, 0)
            self.assertTrue(inst.is_provisioned(d), "confirm wrote the checkpoint")


# ==== VERIFY + RETIRE (core slice 27c) ===============================================================

# The finish (verify + retire) copy adds the saved-information terms to the apply forbidden list.
_FORBIDDEN_FINISH = _FORBIDDEN_APPLY + ("graph", "fingerprint")
_FINISH_KEYS = ("verify-paused", "verify-next-actions", "verify-ok", "verify-gate-on",
                "verify-gate-pending", "retire-success")


def _finished_fixture(tmp, handle="octocat"):
    """Build + confirm + apply a fixture with the first-run assets planted, leaving a fully-installed,
    consistent practice engine ready for verify/retire. Caller holds the surrounding _redirect_root(tmp)."""
    inst._build_fixture(tmp)
    inst._plant_first_run_assets(tmp)
    inst.confirm([], "solo", engine_release="1.0.0", handle=handle)
    return inst._finish_apply(tmp)


class TestVerify(unittest.TestCase):
    def test_clean_setup_passes(self):
        with tempfile.TemporaryDirectory() as d:
            with inst._redirect_root(d):
                _finished_fixture(d)
                res = inst.verify(announce=lambda t: None)
            self.assertFalse(res["paused"])
            self.assertEqual(res["findings"], [])
            self.assertEqual(res["steps"][0]["status"], "ok")

    def test_pauses_on_a_hard_finding_with_both_next_actions(self):
        with tempfile.TemporaryDirectory() as d:
            said = []
            with inst._redirect_root(d):
                _finished_fixture(d)
                inst.wiring.apply(inst._ORPHAN_WIRE)         # a setting belonging to no installed module
                res = inst.verify(announce=said.append)
            self.assertTrue(res["paused"])
            self.assertEqual(len(res["findings"]), 1)
            self.assertEqual(res["steps"][0]["status"], "paused")
            blob = "\n".join(said).lower()
            self.assertIn("run setup again", blob, "the fix-and-retry next action is offered")
            self.assertIn("report it", blob, "the stop-and-report next action is offered (never a dead-end)")

    def test_resumable_after_repair(self):
        with tempfile.TemporaryDirectory() as d:
            with inst._redirect_root(d):
                _finished_fixture(d)
                inst.wiring.apply(inst._ORPHAN_WIRE)
                bad = inst.verify(announce=lambda t: None)
                inst.wiring.reverse(inst._ORPHAN_WIRE)       # the operator fixes it
                good = inst.verify(announce=lambda t: None)
            self.assertTrue(bad["paused"])
            self.assertFalse(good["paused"], "re-running after the repair re-checks clean (resumable)")

    def test_surfaces_gate_on_when_protected(self):
        with tempfile.TemporaryDirectory() as d:
            said = []
            with inst._redirect_root(d):
                _finished_fixture(d)
                inst.verify(announce=said.append, control_status={"protected": True})
            self.assertIn("review gate is on", "\n".join(said).lower())

    def test_surfaces_gate_pending_when_not_protected(self):
        with tempfile.TemporaryDirectory() as d:
            said = []
            with inst._redirect_root(d):
                _finished_fixture(d)
                inst.verify(announce=said.append, control_status={"protected": False})
            self.assertIn("isn't on yet", "\n".join(said).lower())

    def test_standalone_defers_the_gate_surface_to_boot(self):
        # With no review-gate status passed, verify says nothing about the gate — boot owns the standing surface.
        with tempfile.TemporaryDirectory() as d:
            said = []
            with inst._redirect_root(d):
                _finished_fixture(d)
                inst.verify(announce=said.append)            # control_status=None
            self.assertNotIn("review gate", "\n".join(said).lower())


class TestRetire(unittest.TestCase):
    def test_refuses_on_a_hard_finding_and_deletes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            with inst._redirect_root(d):
                _finished_fixture(d)
                inst.wiring.apply(inst._ORPHAN_WIRE)
                res = inst.retire(announce=lambda t: None)
                present = all(os.path.exists(os.path.join(d, rel)) for rel in inst._FIRST_RUN_ASSET_FILES)
            self.assertTrue(res["refused"])
            self.assertEqual(res["reason"], "inconsistent")
            self.assertEqual(res["deleted"], [])
            self.assertTrue(present, "the irreversible tidy-up never runs on an inconsistent setup")

    def test_tidies_assets_and_preserves_the_permanent_set(self):
        with tempfile.TemporaryDirectory() as d:
            with inst._redirect_root(d):
                _finished_fixture(d)
                res = inst.retire(announce=lambda t: None)
                files_gone = all(not os.path.exists(os.path.join(d, rel))
                                 for rel in inst._FIRST_RUN_ASSET_FILES)
                dir_gone = not os.path.isdir(os.path.join(d, ".claude", "skills", "engine-setup"))
                catalog_kept = os.path.isfile(os.path.join(d, ".engine", "provisioning", "module-catalog.json"))
                still_clean = not inst._hard_findings()
            self.assertFalse(res["refused"])
            self.assertTrue(files_gone, "the one-time setup files are removed")
            self.assertTrue(dir_gone, "the setup skill is removed")
            self.assertTrue(catalog_kept, "the catalog the engine keeps is preserved")
            self.assertTrue(still_clean, "the result stays consistent after retire (the deployed repo stays green)")
            self.assertEqual(res["graph"], "regenerated")

    def test_regen_drops_a_stale_tool_entity(self):
        # The load-bearing deployed-repo guarantee: after the tools are gone, the saved information no longer
        # lists them, so the merge-time check stays green. Seed a STALE entry for the to-be-deleted tool and
        # assert retire's re-derive drops it.
        with tempfile.TemporaryDirectory() as d:
            graph_path = os.path.join(d, ".engine", "knowledge", "graph.json")
            with inst._redirect_root(d):
                _finished_fixture(d)
                inst._write_json(graph_path, {"schema_version": 1, "entities": [
                    {"id": "tool:instantiator", "type": "tool", "name": ".engine/tools/instantiator.py"}]})
                inst.retire(announce=lambda t: None)
                with open(graph_path, encoding="utf-8") as fh:
                    regenerated = fh.read()
            self.assertNotIn("tool:instantiator", regenerated,
                             "the re-derive drops the deleted tool from the saved information")

    def test_idempotent_resume(self):
        with tempfile.TemporaryDirectory() as d:
            with inst._redirect_root(d):
                _finished_fixture(d)
                inst.retire(announce=lambda t: None)
                second = inst.retire(announce=lambda t: None)
            self.assertFalse(second["refused"], "a resumed retire is safe")
            self.assertEqual(second["deleted"], [], "the second pass finds everything already gone")
            self.assertTrue(set(inst._FIRST_RUN_ASSET_FILES).issubset(set(second["already_absent"])))


class TestRetireIsolation(unittest.TestCase):
    def test_retire_under_redirect_leaves_real_files_untouched(self):
        # The most dangerous demo: retire deletes its own source. A redirect leak would delete the REAL tool.
        snap = inst._snapshot_real_files()
        real_self = os.path.join(inst.validate.ROOT, ".engine", "tools", "instantiator.py")
        with tempfile.TemporaryDirectory() as d:
            with inst._redirect_root(d):
                _finished_fixture(d)
                inst.retire(announce=lambda t: None)
        self.assertTrue(inst._assert_real_files_unchanged(snap), "a redirected retire must not touch real files")
        self.assertTrue(os.path.isfile(real_self), "the real setup tool must survive a redirected retire")


class TestFinishCopy(unittest.TestCase):
    def test_template_carries_every_finish_section(self):
        copy = inst.load_copy(inst.TEMPLATE_PATH)
        for key in _FINISH_KEYS:
            self.assertTrue(copy[key].strip(), f"finish copy section {key!r} missing from the template")

    def test_finish_copy_is_plain_language(self):
        copy = inst.load_copy(inst.TEMPLATE_PATH)
        blob = "\n".join(copy[k] for k in _FINISH_KEYS).lower()
        for term in _FORBIDDEN_FINISH:
            self.assertNotIn(term, blob, f"plain-language law: '{term}' must not surface in the finish copy")


class TestFinishDemoRunsGreen(unittest.TestCase):
    def test_finish_demo_exits_zero(self):
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = inst.main(["finish-demo"])
        out = buf.getvalue()
        self.assertEqual(rc, 0, out)
        self.assertIn("byte-for-byte unchanged", out, "the isolation guarantee is shown")
        self.assertIn("still exists: True", out, "the demo proves the real setup tool survives its own tidy-up")
        self.assertIn("naming it, not hiding it", out, "the honest-ceiling banner leads the demo")


class TestFinishCli(unittest.TestCase):
    def _silent(self):
        import contextlib, io
        return contextlib.redirect_stdout(io.StringIO())

    def test_verify_verb_exits_zero_on_clean(self):
        with tempfile.TemporaryDirectory() as d:
            with inst._redirect_root(d), self._silent():
                _finished_fixture(d)
                rc = inst.main(["verify"])
            self.assertEqual(rc, 0)

    def test_verify_verb_exits_one_on_a_hard_finding(self):
        with tempfile.TemporaryDirectory() as d:
            with inst._redirect_root(d), self._silent():
                _finished_fixture(d)
                inst.wiring.apply(inst._ORPHAN_WIRE)
                rc = inst.main(["verify"])
            self.assertEqual(rc, 1, "a hard finding makes the verify verb exit non-zero")

    def test_retire_verb_completes_on_clean(self):
        with tempfile.TemporaryDirectory() as d:
            with inst._redirect_root(d), self._silent():
                _finished_fixture(d)
                rc = inst.main(["retire"])
            self.assertEqual(rc, 0)
            self.assertFalse(os.path.exists(os.path.join(d, ".engine", "tools", "instantiator.py")))


if __name__ == "__main__":
    unittest.main()
