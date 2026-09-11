#!/usr/bin/env python3
"""Import drift proofs over miniature candidate trees and real registered entry points."""
from __future__ import annotations

import ast
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import enforcement_files_check
import weakening_guard as guard
import module_coherence
import module_manager
import validate



class TestReviewerContractRegistration(unittest.TestCase):
    def test_shared_owner_is_guarded_and_its_surfaces_ship_with_core(self):
        root = Path(__file__).resolve().parents[2]
        self.assertEqual([], guard.enforcement_drift_findings(str(root)))
        covered = guard._derive_check_scripts(str(root / ".engine/check"))
        self.assertIsNotNone(covered)
        self.assertIn(".engine/tools/reviewer_contracts.py", covered)
        manifest = json.loads((root / ".engine/modules/core/manifest.json").read_text())
        import fnmatch
        for kind, path in (("tool", ".engine/tools/reviewer_contracts.py"),
                           ("tool", ".engine/tools/test_reviewer_contracts.py"),
                           ("schema", ".engine/schemas/reviewer-contract.v1.json")):
            self.assertTrue(any(fnmatch.fnmatchcase(path, pattern)
                                for pattern in manifest["provides"][kind]), path)


class TestImportExtraction(unittest.TestCase):
    INDEX = {'pkg': '.engine/tools/pkg/__init__.py',
             'pkg.child': '.engine/tools/pkg/child.py',
             'pkg.sub': '.engine/tools/pkg/sub/__init__.py',
             'pkg.sub.leaf': '.engine/tools/pkg/sub/leaf.py',
             'gate': '.engine/tools/gate.py'}

    def scan(self, text, source='.engine/tools/pkg/check.py'):
        return guard._ast_import_edges(source, text, self.INDEX)

    def test_direct_and_conditional_imports_include_real_package_initializers(self):
        edges, _, unsupported, unresolved = self.scan('if enabled:\n import pkg.sub.leaf as gate\n')
        self.assertEqual(edges, {self.INDEX[x] for x in ('pkg', 'pkg.sub', 'pkg.sub.leaf')})
        self.assertEqual((unsupported, unresolved), ([], []))

    def test_from_imports_resolve_modules_and_leave_attribute_exports_to_review(self):
        edges, _, _, unresolved = self.scan('from pkg import child\nfrom gate import exported_name\n')
        self.assertEqual(edges, {self.INDEX[x] for x in ('pkg', 'pkg.child', 'gate')})
        self.assertEqual(unresolved, [])

    def test_relative_imports_and_real_parent_initializers(self):
        edges, _, _, unresolved = self.scan('from .. import child\nfrom . import leaf\n',
                                            '.engine/tools/pkg/sub/check.py')
        self.assertEqual(edges, {self.INDEX[x] for x in ('pkg', 'pkg.child', 'pkg.sub', 'pkg.sub.leaf')})
        self.assertEqual(unresolved, [])

    def test_namespace_package_needs_no_invented_initializer(self):
        edges, _, _, unresolved = guard._ast_import_edges('.engine/tools/ns/check.py',
            'from . import child\n', {'ns.child': '.engine/tools/ns/child.py'})
        self.assertEqual(edges, {'.engine/tools/ns/child.py'})
        self.assertEqual(unresolved, [])

    def test_unknown_missing_and_beyond_top_imports_fail_visibly(self):
        for text in ('import missing_gate', 'import pkg.missing', 'from pkg.missing import name',
                     'from ... import child', 'from . import child'):
            with self.subTest(text=text):
                source = '.engine/tools/check.py' if text == 'from . import child' else '.engine/tools/pkg/check.py'
                self.assertTrue(self.scan(text, source)[3])

    def test_literal_loaders_support_aliases_relative_packages_and_external_modules(self):
        text = ('import importlib as il\nfrom importlib import import_module as load\n'
                'again = load\nagain("..child", "pkg.sub")\nil.import_module("json")\n'
                '__import__("gate")\n')
        edges, literal, unsupported, unresolved = self.scan(text)
        self.assertEqual(edges, {self.INDEX[x] for x in ('pkg', 'pkg.child', 'gate')})
        self.assertEqual(len(literal), 3)
        self.assertEqual((unsupported, unresolved), ([], []))

    def test_unrelated_scope_cannot_erase_loader_or_module_aliases(self):
        variants = (
            'import importlib\ndef unrelated():\n import json as importlib\nimportlib.import_module("gate")',
            'import importlib as il\ndef unrelated():\n import json as il\nil.import_module("gate")',
            'import importlib\nloader = importlib\nloader.import_module("gate")',
            'from importlib import import_module as load\ndef unrelated():\n from json import loads as load\nload("gate")',
        )
        for text in variants:
            with self.subTest(text=text):
                edges, literal, unsupported, unresolved = self.scan(text)
                self.assertIn(self.INDEX['gate'], edges)
                self.assertEqual(len(literal), 1)
                self.assertEqual((unsupported, unresolved), ([], []))

    def test_getattr_loader_construction_cannot_hide_local_imports(self):
        for text in (
            'import importlib\nloader = getattr(importlib, "import_module")\nloader("gate")',
            'import importlib\ngetattr(importlib, "import_module")("gate")',
            'import importlib as il\nfrom builtins import getattr as grab\nloader = grab(il, "import_module")\nloader("gate")',
        ):
            with self.subTest(text=text):
                edges, literal, unsupported, unresolved = self.scan(text)
                self.assertIn(self.INDEX['gate'], edges)
                self.assertEqual(len(literal), 1)
                self.assertEqual((unsupported, unresolved), ([], []))

    def test_dynamic_getattr_loader_is_an_explicit_call_multiset(self):
        for text in (
            'import importlib\nloader = getattr(importlib, method)\nloader("gate")\nloader("gate")',
            'import importlib\ngetattr(importlib, method)("gate")\ngetattr(importlib, method)("gate")',
        ):
            with self.subTest(text=text):
                _, literal, unsupported, unresolved = self.scan(text)
                expected = 3 if 'loader =' in text else 4
                self.assertEqual(len(unsupported), expected)
                selector = ast.dump(ast.parse('getattr(importlib, method)').body[0].value,
                                    include_attributes=False)
                self.assertIn(selector, unsupported)
                self.assertEqual((literal, unresolved), ([], []))

    def test_standard_identity_cannot_suppress_nonstandard_loader_exception(self):
        text = ('from runpy import run_path as load\n'
                'def unused():\n from importlib import import_module as load\n'
                'load("json")\n')
        _, literal, unsupported, unresolved = self.scan(text)
        self.assertEqual((literal, unresolved), ([], []))
        self.assertEqual(unsupported, [ast.dump(ast.parse('load("json")').body[0].value,
                                              include_attributes=False)])

    def test_known_and_dynamic_loader_identity_keeps_both_expressions_reviewed(self):
        text = ('import importlib\nloader = importlib.import_module\n'
                'loader = getattr(importlib, method)\nloader("gate")\n')
        _, literal, unsupported, unresolved = self.scan(text)
        expected = [ast.dump(ast.parse(call).body[0].value, include_attributes=False)
                    for call in ('getattr(importlib, method)', 'loader("gate")')]
        self.assertEqual(unsupported, sorted(expected))
        self.assertEqual((literal, unresolved), ([], []))

    def test_unresolved_literal_loader_is_not_silently_ignored(self):
        self.assertEqual(self.scan('__import__("missing_gate")')[3], ['missing_gate'])

    def test_dynamic_and_nonstandard_loader_aliases_preserve_call_duplicates(self):
        text = ('from importlib.util import spec_from_file_location as spec\n'
                'from importlib.machinery import SourceFileLoader as Loader\n'
                'from runpy import run_path as run\nimport builtins\nload = builtins.__import__\n'
                'load(name)\nload(name)\nspec(name, path)\nLoader(name, path).load_module()\nrun(path)\n')
        _, _, unsupported, unresolved = self.scan(text)
        self.assertEqual(len(unsupported), 6)
        self.assertEqual(unsupported.count(ast.dump(ast.parse('load(name)').body[0].value,
                                                  include_attributes=False)), 2)
        self.assertEqual(unresolved, [])

    def test_unresolved_loader_arguments_are_reviewed_as_whole_calls(self):
        for call in ('__import__("child", globals(), locals(), [], 1)',
                     'il.import_module(".child", package=pkg)',
                     'il.import_module(".child")', 'il.import_module(*names)'):
            with self.subTest(call=call):
                _, literal, unsupported, _ = self.scan('import importlib as il\n' + call)
                self.assertFalse(literal)
                self.assertEqual(len(unsupported), 1)

    def test_module_index_prefers_package_and_preserves_absent_exclusions(self):
        with tempfile.TemporaryDirectory() as root:
            tools = Path(root, '.engine/tools')
            (tools / 'pkg').mkdir(parents=True)
            (tools / 'pkg.py').write_text('')
            (tools / 'pkg/__init__.py').write_text('')
            inventory = {'.engine/tools/root.py': {'dependencies': (),
                         'exclusions': {'.engine/tools/optional/helper.py': 'separate optional path'}}}
            index = guard._tool_module_index(root, inventory)
            self.assertEqual(index['pkg'], '.engine/tools/pkg/__init__.py')
            self.assertEqual(index['optional.helper'], '.engine/tools/optional/helper.py')


class TestCandidateInventory(unittest.TestCase):
    SOURCE = '.engine/tools/root.py'

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.tools = self.root / '.engine/tools'
        self.checks = self.root / '.engine/check'
        self.tools.mkdir(parents=True)
        self.checks.mkdir()
        self.roots = {'engine/check/root': self.SOURCE}
        self.inventory = {self.SOURCE: {'dependencies': (), 'exclusions': {}}}
        self.loaders = {}
        self.rule = {'id': 'engine/check/root', 'kind': 'custom/script', 'tier': 'hard',
                     'params': {'script': self.SOURCE}}
        self.source = 'pass\n'

    def write(self):
        (self.tools / 'root.py').write_text(self.source)
        (self.tools / 'weakening_guard.py').write_text(
            '_HARD_SCRIPT_ROOTS = ' + repr(self.roots) + '\nENFORCEMENT_SOURCE_INVENTORY = '
            + repr(self.inventory) + '\nENFORCEMENT_DYNAMIC_LOADERS = ' + repr(self.loaders) + '\n')
        (self.checks / 'root.json').write_text(json.dumps(self.rule))

    def findings(self):
        self.write()
        return guard.enforcement_drift_findings(str(self.root))

    def test_positive_and_planted_extraction_controls(self):
        self.assertEqual(self.findings(), [])
        self.source = 'import planted\n'
        (self.tools / 'planted.py').write_text('pass\n')
        self.assertIn('unclassified', ' '.join(self.findings()))
        self.inventory[self.SOURCE]['dependencies'] = ('.engine/tools/planted.py',)
        self.inventory['.engine/tools/planted.py'] = {'dependencies': (), 'exclusions': {}}
        self.assertEqual(self.findings(), [])
        self.source = 'pass\n'
        self.assertIn('stale', ' '.join(self.findings()))

    def test_alias_masking_cannot_hide_unclassified_extraction(self):
        (self.tools / 'planted.py').write_text('pass\n')
        for prefix in ('import importlib\n',
                       'import importlib\ndef unused():\n import json as importlib\n'):
            self.source = prefix + 'importlib.import_module("planted")\n'
            self.assertIn('unclassified', ' '.join(self.findings()))

    def test_getattr_cannot_hide_unclassified_extraction(self):
        (self.tools / 'planted.py').write_text('pass\n')
        self.source = 'import importlib\nloader = getattr(importlib, "import_module")\nloader("planted")\n'
        self.assertIn('unclassified', ' '.join(self.findings()))

    def test_in_tools_symlink_cannot_hide_unguarded_implementation(self):
        self.write()
        (self.tools / 'implementation.py').write_text('pass\n')
        (self.tools / 'root.py').unlink()
        (self.tools / 'root.py').symlink_to('implementation.py')
        self.assertIn('symbolic-link', ' '.join(guard.enforcement_drift_findings(str(self.root))))
        with mock.patch.object(guard, '_HARD_SCRIPT_ROOTS', self.roots), \
             mock.patch.object(guard, 'ENFORCEMENT_SOURCE_INVENTORY', self.inventory), \
             mock.patch.object(guard, 'ENFORCEMENT_DYNAMIC_LOADERS', {}), \
             mock.patch.object(guard, '_BASE_CHECK_DIR', str(self.checks)):
            self.assertIsNone(guard._derive_check_scripts())
            self.assertTrue(guard.flagged_changes([{'filename': '.engine/tools/implementation.py',
                                                   'status': 'modified'}]))

    def test_exclusions_stop_recursion_and_optional_absence_is_valid(self):
        optional = '.engine/tools/optional/helper.py'
        self.source = 'from optional import helper\n'
        self.inventory[self.SOURCE]['exclusions'] = {optional: 'optional reporting path'}
        self.roots['engine/check/optional'] = optional
        self.inventory[optional] = {'dependencies': (), 'exclusions': {}}
        self.assertEqual(self.findings(), [])
        (self.tools / 'optional').mkdir()
        (self.tools / 'optional/helper.py').write_text('deliberately invalid syntax !')
        self.assertEqual(self.findings(), [])

    def test_missing_mismatched_and_new_active_roots_fail(self):
        for roots, script in (({}, self.SOURCE), (self.roots, '.engine/tools/moved.py'),
                              (self.roots, None)):
            with self.subTest(roots=roots, script=script):
                original = self.roots
                self.roots = roots
                self.rule['params']['script'] = script
                self.assertTrue(self.findings())
                self.roots = original

    def test_new_root_finding_names_the_declarations_and_recheck(self):
        self.roots = {}
        message = ' '.join(self.findings())
        for text in ('_HARD_SCRIPT_ROOTS', 'ENFORCEMENT_SOURCE_INVENTORY',
                     '.engine/tools/weakening_guard.py', 'rerun'):
            self.assertIn(text, message)

    def test_duplicate_active_rule_and_unreadable_json_fail(self):
        self.write()
        (self.checks / 'duplicate.json').write_text(json.dumps(self.rule))
        self.assertIn('duplicate', ' '.join(guard.enforcement_drift_findings(str(self.root))))
        (self.checks / 'duplicate.json').write_text('{broken')
        self.assertIn('invalid', ' '.join(guard.enforcement_drift_findings(str(self.root))))

    def test_missing_file_malformed_ast_and_malformed_inventory_fail(self):
        self.source = 'def :\n'
        self.assertIn('cannot read or parse AST', ' '.join(self.findings()))
        (self.tools / 'root.py').unlink()
        self.assertIn('missing', ' '.join(guard.enforcement_drift_findings(str(self.root))))
        self.inventory[self.SOURCE]['dependencies'] = ('../escape.py',)
        self.assertIn('invalid', ' '.join(self.findings()))

    def test_candidate_declaration_duplicates_are_not_collapsed(self):
        for suffix in ('\n_HARD_SCRIPT_ROOTS = {}\n',
                       '\nENFORCEMENT_DYNAMIC_LOADERS += {}\n'):
            self.write()
            with (self.tools / 'weakening_guard.py').open('a') as fh:
                fh.write(suffix)
            self.assertIn('more than once', ' '.join(guard.enforcement_drift_findings(str(self.root))))
        self.write()
        path = self.tools / 'weakening_guard.py'
        path.write_text(path.read_text().replace('ENFORCEMENT_DYNAMIC_LOADERS = {}',
                                                'ENFORCEMENT_DYNAMIC_LOADERS = {"x": {}, "x": {}}'))
        self.assertIn('duplicate key', ' '.join(guard.enforcement_drift_findings(str(self.root))))

    def test_loader_multiset_detects_changes_removal_and_duplicate_calls(self):
        self.source = '__import__(module_name)\n'
        call = ast.dump(ast.parse(self.source).body[0].value, include_attributes=False)
        self.assertIn('multiset', ' '.join(self.findings()))
        self.loaders = {self.SOURCE: {'source': self.SOURCE, 'reason': 'runtime plugin selection', 'calls': [call]}}
        self.assertEqual(self.findings(), [])
        for source in ('pass\n', '__import__(different_name)\n', self.source * 2):
            self.source = source
            self.assertIn('multiset', ' '.join(self.findings()))
        self.loaders[self.SOURCE]['calls'] = [call, call]
        self.source = '__import__(module_name)\n' * 2
        self.assertEqual(self.findings(), [])

    def test_changed_dynamic_selector_invalidates_reviewed_loader_multiset(self):
        self.source = 'import importlib\nloader = getattr(importlib, method)\nloader("json")\n'
        calls = guard._ast_import_edges(self.SOURCE, self.source, {})[2]
        self.assertEqual(len(calls), 2)
        self.loaders = {self.SOURCE: {'source': self.SOURCE,
            'reason': 'Explicit runtime selection exercised by this fixture', 'calls': calls}}
        self.assertEqual(self.findings(), [])
        self.source = self.source.replace('method)', 'other_method)')
        self.assertTrue(self.findings())

    def test_mixed_loader_identity_cannot_bypass_candidate_exception(self):
        self.source = ('from runpy import run_path as load\n'
            'def unused():\n from importlib import import_module as load\nload("json")\n')
        self.assertTrue(self.findings())

    def test_malformed_loader_data_is_a_finding_never_a_crash(self):
        for loaders in ([], {self.SOURCE: []}, {self.SOURCE: {'source': self.SOURCE, 'reason': '', 'calls': []}},
                        {self.SOURCE: {'source': self.SOURCE, 'reason': 'why', 'calls': [1, 'Call(x)']}}):
            self.loaders = loaders
            self.assertIn('invalid', ' '.join(self.findings()))

    def test_scanned_candidate_guard_and_source_are_never_executed(self):
        marker = self.root / 'executed'
        hostile = f'open({str(marker)!r}, "w").write("executed")\nraise RuntimeError("executed")\n'
        self.source = hostile
        self.write()
        with (self.tools / 'weakening_guard.py').open('a') as fh:
            fh.write(hostile)
        self.assertEqual(guard.enforcement_drift_findings(str(self.root)), [])
        self.assertFalse(marker.exists())

    def test_source_symlink_escape_fails_before_scanning(self):
        self.write()
        outside = self.root / 'outside.py'
        outside.write_text('pass\n')
        (self.tools / 'root.py').unlink()
        (self.tools / 'root.py').symlink_to(outside)
        self.assertIn('escapes', ' '.join(guard.enforcement_drift_findings(str(self.root))))


class TestEntryPoints(unittest.TestCase):
    def test_malformed_loader_declarations_keep_guard_coverage_conservative(self):
        with mock.patch.object(guard, 'ENFORCEMENT_DYNAMIC_LOADERS', []):
            self.assertIsNone(guard._derive_check_scripts())
            self.assertTrue(guard.is_guardrail('.engine/tools/ordinary.py'))

    def test_help_never_reaches_action_or_environment_override(self):
        for argv in (['--help'], ['bad', '-h'], ['demo', '--help']):
            with self.subTest(argv=argv), mock.patch.object(enforcement_files_check, '_main',
                    side_effect=AssertionError('action ran')), mock.patch.object(
                    enforcement_files_check.validate, 'env_override_path', side_effect=AssertionError('env read')):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(enforcement_files_check.main(argv), 0)
                self.assertIn('Usage:', output.getvalue())

    def test_live_check_and_shipped_negative_fixture(self):
        root = Path(__file__).resolve().parents[2]
        command = [sys.executable, str(root / '.engine/tools/enforcement_files_check.py')]
        environment = dict(os.environ)
        environment.pop('ENGINE_ENFORCEMENT_FILES_ROOT', None)
        environment['ENGINE_RULE_TIER'] = 'hard'
        clean = subprocess.run(command, cwd=root, env=environment, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(clean.stdout), [])
        fixture = root / '.engine/_fixtures/enforcement-files'
        target = json.loads((fixture / 'target.json').read_text())
        environment.update(target['env'])
        broken = subprocess.run(command, cwd=root, env=environment, capture_output=True, text=True, check=True)
        expectation = json.loads((fixture / 'expect.json').read_text())
        self.assertTrue(any(f['severity'] == expectation['severity'] and expectation['message_contains'] in f['message']
                            for f in json.loads(broken.stdout)), broken.stdout)

    def _projected_tree(self, source_root: Path, destination_root: Path, manifests):
        """Copy exactly the manager's delivered map into an otherwise empty deployment tree."""
        by_id = {manifest['id']: manifest for _path, manifest in manifests}
        for relative, source in module_manager.engine_synced_map(
                str(source_root), by_id, project_retire=True).items():
            destination = destination_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

    def test_minimal_and_full_deliveries_run_wrapper_and_registered_fixture(self):
        source_root = Path(__file__).resolve().parents[2]
        source_manifests = module_coherence.discover_manifests(str(source_root))
        variants = {
            'minimal': [(path, manifest) for path, manifest in source_manifests
                        if manifest.get('status') == 'required'],
            'full': source_manifests,
        }
        for name, manifests in variants.items():
            with self.subTest(delivery=name), tempfile.TemporaryDirectory() as raw:
                projected = Path(raw)
                self._projected_tree(source_root, projected, manifests)
                fixture = projected / '.engine/_fixtures/enforcement-files'
                self.assertTrue(fixture.is_dir(), 'the shipped negative fixture must be delivered')
                wrapper = [sys.executable, str(projected / '.engine/tools/enforcement_files_check.py')]
                environment = dict(os.environ, ENGINE_RULE_TIER='hard')
                environment.pop('ENGINE_ENFORCEMENT_FILES_ROOT', None)
                clean = subprocess.run(wrapper, cwd=projected, env=environment,
                                       capture_output=True, text=True, check=True)
                self.assertEqual(json.loads(clean.stdout), [], clean.stdout)

                target = json.loads((fixture / 'target.json').read_text())
                expectation = json.loads((fixture / 'expect.json').read_text())
                environment.update(target['env'])
                broken = subprocess.run(wrapper, cwd=projected, env=environment,
                                        capture_output=True, text=True, check=True)
                self.assertTrue(any(f['severity'] == expectation['severity']
                                    and expectation['message_contains'] in f['message']
                                    for f in json.loads(broken.stdout)), broken.stdout)

                rule = json.loads((projected / '.engine/check/enforcement-files.json').read_text())
                original_root = validate.ROOT
                try:
                    validate.ROOT = str(projected)
                    passed, findings = validate.run_unit(rule, {'env': target['env']}, {})
                finally:
                    validate.ROOT = original_root
                self.assertFalse(passed)
                self.assertTrue(any(f['severity'] == 'hard'
                                    and expectation['message_contains'] in f['message'] for f in findings), findings)


if __name__ == '__main__':
    unittest.main()
