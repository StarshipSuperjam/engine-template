"""Bounded lifecycle permission controls over the production cost assessment owner."""
import copy
import unittest

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import build_coordinator_work as work
import selftest_cost as cost
from test_selftest_performance import cost_example, cost_counts


@cost.declaration({
    "schema_version": "test-cost-contract.v1", "supported_fault": "Cached resource permission survives exception expiry",
    "boundary": "pure", "boundary_rationale": "Exercise retained observations with explicit clock values",
    "fixture_owner": "test_build_coordinator_cost.LivePermission", "dependencies": ["jsonschema", "test_selftest_performance.cost_example"],
    "data_reads": [".engine/schemas/*.json", ".engine/policies/test-cost.json"], "cadence": "pr",
    "limits": {**cost.zeros(), "schema_decodes": 40, "metaschema_validations": 0},
    "mutable_state": "Fresh bounded dictionaries per case", "cache_lifetime": "case",
    "added_cost_risk": "At most three assessments of one measured case", "families": []})
class LivePermission(unittest.TestCase):
    def setUp(self):
        self.observation, self.context = cost_example()
        self.context.pop("now", None)
        self.identity = self.context["expected_identity"]
        self.exception = {
            "id": "temporary-process", "owner": "fixture maintainer",
            "reason": "One process proves the boundary", "revisit": "Remove after boundary repair",
            "issued_at": "2026-09-13T01:00:00Z", "expires_at": "2026-09-13T02:00:00Z",
            "case": self.observation["cases"][0]["case"], "resource": "processes", "ceiling": 1,
            "source_commit": self.identity["source_commit"], "supported_fault": "Real child failure",
            "fault_preservation_evidence": "Retained one-process child failure witness"}

    def assess(self, evidence, at, exceptions=()):
        return work.assess_retained_cost(evidence, expected_identity=self.identity, now=at, exceptions=exceptions)

    def test_expiry_invalidates_permission_without_rewriting_the_observation(self):
        observation = cost_counts(self.observation, "processes", 1)
        evidence = work.retain_cost(observation, self.context)
        original = copy.deepcopy(evidence)
        active = self.assess(evidence, "2026-09-13T01:59:59Z", [self.exception])
        self.assertEqual([], active["violations"])
        expired = self.assess(evidence, "2026-09-13T02:00:00Z", [self.exception])
        self.assertTrue(expired["violations"])
        self.assertNotEqual(cost.digest(active), cost.digest(expired))
        self.assertEqual(original, evidence)

    def test_expired_unneeded_allowance_preserves_unwaived_success(self):
        evidence = work.retain_cost(self.observation, self.context)
        before = self.assess(evidence, "2026-09-13T01:59:59Z", [self.exception])
        after = self.assess(evidence, "2026-09-13T02:00:00Z", [self.exception])
        self.assertEqual([], after["violations"])
        self.assertEqual(before, after)

    def test_observed_bytes_and_controller_identity_both_have_to_match(self):
        evidence = work.retain_cost(self.observation, self.context)
        altered = copy.deepcopy(evidence)
        altered["observation"]["totals"]["processes"] = 100
        with self.assertRaises(work.CoordinatorError):
            self.assess(altered, "2026-09-13T01:30:00Z")
        for key, value in (("source_commit", "f" * 40), ("attempt", "other-attempt"), ("stage", "node-focused")):
            with self.subTest(key=key), self.assertRaises(work.CoordinatorError):
                work.assess_retained_cost(evidence, expected_identity={**self.identity, key: value},
                                          now="2026-09-13T01:30:00Z")
        with self.assertRaises(work.CoordinatorError):
            work.retain_cost(self.observation, {**self.context, "now": "2026-09-13T01:30:00Z"})

    def test_implementation_declarations_cannot_raise_the_approved_node_budget(self):
        from test_selftest_performance import _COST_CONTRACT
        self.context['runtime'][0]['contract'] = copy.deepcopy(_COST_CONTRACT)
        contract = copy.deepcopy(_COST_CONTRACT)
        contract['limits']['schema_decodes'] = 1
        approval = {'contract': contract, 'cases': [self.observation['cases'][0]['case']]}
        evidence = work.retain_cost(self.observation, self.context, node_approval=approval)
        verdict = self.assess(evidence, '2026-09-13T01:30:00Z')
        self.assertTrue(any('more cost than the approved node' in v for v in verdict['violations']))
        self.assertFalse(verdict['cost_clearance'])
        approval['contract']['limits'] = copy.deepcopy(_COST_CONTRACT['limits'])
        approved = work.retain_cost(self.observation, self.context, node_approval=approval)
        self.assertEqual([], self.assess(approved, '2026-09-13T01:30:00Z')['violations'])

    def test_actual_base_identity_catches_removal_of_a_case_added_after_debt_enrollment(self):
        base = {'cases': copy.deepcopy(self.context['baseline']['cases']), 'duplicates': []}
        added_since_enrollment = copy.deepcopy(base['cases'][0])
        added_since_enrollment['case']['id'] = 'test_example.C.test_later_release'
        added_since_enrollment['qualified_name'] = 'C.test_later_release'
        base['cases'].append(added_since_enrollment)
        self.context.pop('base_observation')
        self.context.pop('expected_base_identity')
        self.context['timing_pairs'] = []
        evidence = work.retain_cost(self.observation, self.context, base_inventory=base)
        verdict = self.assess(evidence, '2026-09-13T01:30:00Z')
        self.assertTrue(any('removed case lacks fault-preservation' in v for v in verdict['violations']))
        self.assertEqual([added_since_enrollment['case']], verdict['removed'])
        self.assertEqual([self.observation['cases'][0]['case']], verdict['common'])
        self.assertIsNone(verdict['aggregate_delta'])
        self.assertIsNone(verdict['base_identity'])


@cost.declaration({
    "schema_version": "test-cost-contract.v1", "supported_fault": "Node integration bypasses actual cost measurement",
    "boundary": "integration", "boundary_rationale": "Two real Git nodes and the existing serial launcher exercise the lifecycle",
    "fixture_owner": "test_build_coordinator_cost.SerialLifecycle", "dependencies": ["git", "selftest", "build_coordinator"],
    "data_reads": [".engine/tools/selftest*.py", ".engine/tools/providers.py", ".engine/tools/mutation_guards.py",
                   ".github/workflows/engine-ci.yml", ".engine/schemas/*.json", ".engine/policies/test-cost.json"],
    "cadence": "pr", "limits": {**cost.zeros(), "processes": 180, "git_commands": 160,
                                    "schema_decodes": 360, "metaschema_validations": 1, "nested_journeys": 5},
    "mutable_state": "One disposable Git repository, private Build state, and retained child reports",
    "cache_lifetime": "case", "added_cost_risk": "Five one-case child runs and bounded base discovery; no whole Engine clone",
    "families": []})
class SerialLifecycle(unittest.TestCase):
    def test_two_real_nodes_measure_before_integration_then_assess_the_candidate(self):
        import argparse
        import contextlib
        import io
        import json
        import os
        from pathlib import Path
        import shutil
        import subprocess
        import sys
        import tempfile
        from unittest.mock import patch
        import build_coordinator as bc
        import build_coordinator_core as core
        from test_build_coordinator import plan_v2, _work_item_v2
        from test_selftest_performance import _COST_CONTRACT
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            root = folder / 'repo'; root.mkdir()
            tools = root / '.engine/tools'; tools.mkdir(parents=True)
            schemas = root / '.engine/schemas'; schemas.mkdir()
            policies = root / '.engine/policies'; policies.mkdir()
            workflow = root / '.github/workflows'; workflow.mkdir(parents=True)
            shutil.copyfile(cost.ROOT / '.github/workflows/engine-ci.yml', workflow / 'engine-ci.yml')
            for name in ('selftest.py', 'selftest_results.py', 'selftest_cost.py', 'providers.py', 'mutation_guards.py'):
                shutil.copyfile(cost.ROOT / '.engine/tools' / name, tools / name)
            for path in (cost.ROOT / '.engine/schemas').glob('*.json'):
                if path.name.startswith(('selftest-', 'test-cost-')):
                    shutil.copyfile(path, schemas / path.name)
            shutil.copyfile(cost.ROOT / '.engine/policies/test-cost.json', policies / 'test-cost.json')
            (root / '.gitignore').write_text('__pycache__/\n')
            contract = {**_COST_CONTRACT, 'limits': cost.zeros(), 'dependencies': [], 'data_reads': []}
            test_source = ('"""A helper must keep its declared process cost."""\nimport unittest, helper, selftest_cost as cost\n'
                           '@cost.declaration(' + repr(contract) + ')\n'
                           'class C(unittest.TestCase):\n def test_behavior(self): helper.run()\n')
            (tools / 'test_example.py').write_text(test_source)
            helper = tools / 'helper.py'; helper.write_text('def run(): return 0\n')
            env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
            env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
            def git(*args):
                return subprocess.check_output(['git', '-C', str(root), *args], env=env,
                    stderr=subprocess.PIPE, text=True).strip()
            git('init', '-q'); git('config', 'user.email', 'fixture@example.invalid'); git('config', 'user.name', 'Fixture')
            git('add', '.'); git('commit', '-qm', 'Original fixture')
            base = git('rev-parse', 'HEAD')
            nodes = [_work_item_v2('one', [], executor='integrator'), _work_item_v2('two', ['one'], executor='integrator')]
            for node in nodes:
                node.update(paths=['.engine/tools/helper.py'], test_cost=contract)
            plan = plan_v2(items=nodes); plan['schema_version'] = 'build-plan.v3'
            plan_path = folder / 'plan.json'; plan_path.write_text(json.dumps(plan))
            store = bc.StateStore(folder / 'build.json')
            protocol = {'validation_commands': {'candidate': [{'id': 'selftests', 'command': [sys.executable,
                str(tools / 'selftest.py'), '--start-dir', str(tools), '--run-record-path', '{run_record_path}']}], 'final': {'context': 'engine-ci'}}}
            with patch.object(bc, 'ROOT', root), patch.object(bc, '_run', side_effect=lambda argv, **kw: core.run(argv,
                    root=kw.get('cwd', root), input_value=kw.get('input_text'))), \
                 patch.object(bc, '_bindings', return_value={}), patch.object(bc, '_read_now'), \
                 patch.object(bc, '_derived_drift', return_value=[]), patch.object(bc, '_merge_base', return_value=base), \
                 patch.object(bc, '_protocol', return_value=protocol), patch.dict(os.environ, env, clear=True), \
                 contextlib.redirect_stdout(io.StringIO()):
                state = bc._initial_state('fixture/repo', 1, base, 'pln_0123456789ab', 'sha256:' + 'a' * 64, plan, None)
                state['approval'] = {'plan_digest': bc._digest(plan), 'spec_digest': None, 'depth': 'thorough'}
                store.create(state)
                for index, node in enumerate(nodes, 1):
                    bc.cmd_work_claim(argparse.Namespace(item=node['id'], provider='codex', plan=str(plan_path), worktree=str(root)), store)
                    attempt = store.read()['work'][node['id']]['claim']['attempt_id']
                    bad = index == 2
                    helper.write_text('import subprocess,sys\ndef run(): subprocess.run([sys.executable,"-c","pass"],check=True)\n'
                                      if bad else 'def run(): return 1\n')
                    git('add', '.engine/tools/helper.py')
                    report = {'outcome': 'returned', 'evidence': {'changed_paths': ['.engine/tools/helper.py'],
                        'verification_results': [{'command': 'serial fixture', 'outcome': 'passed', 'detail': 'Fixture setup'}],
                        'assumptions': [], 'unresolved_concerns': []}}
                    result_path = folder / 'result.json'; result_path.write_text(json.dumps(report))
                    result_args = argparse.Namespace(item=node['id'], attempt=attempt, plan=str(plan_path), input=str(result_path))
                    bc.cmd_work_result(result_args, store)
                    git('commit', '-qm', 'Node ' + str(index))
                    args = argparse.Namespace(item=node['id'], attempt=attempt, plan=str(plan_path),
                        commit=git('rev-parse', 'HEAD'), verification_input='Observed serial fixture', recovery=False)
                    with self.assertRaises(core.CoordinatorError):
                        bc.cmd_work_integrate(args, store)
                    if bad:
                        with self.assertRaisesRegex(core.CoordinatorError, 'cost violations'):
                            bc.cmd_work_verify(args, store)
                        self.assertNotIn(node['id'], [n['id'] for n in store.read()['progress']['completed']])
                        helper.write_text('def run(): return 2\n')
                        git('add', '.engine/tools/helper.py')
                        bc.cmd_work_result(result_args, store)
                        git('commit', '-qm', 'Repair helper cost')
                        args.commit = git('rev-parse', 'HEAD')
                    bc.cmd_work_verify(args, store)
                    bc.cmd_work_integrate(args, store)
                self.assertEqual(2, len(store.read()['progress']['completed']))
                self.assertEqual(test_source, (tools / 'test_example.py').read_text())
                bc.cmd_validate(argparse.Namespace(mode='candidate', plan=str(plan_path), force=False), store)
                current = store.read()
                self.assertEqual('candidate', current['cost']['candidate']['identity']['stage'])
                self.assertTrue(all(ref['identity']['stage'] == 'node-focused' for ref in current['cost']['nodes'].values()))
                self.assertIsNone(current['validation']['final'])
                assessment = bc._live_candidate_cost(current, git('rev-parse', 'HEAD'))
                self.assertEqual([], assessment['violations'])
                self.assertFalse(assessment['cost_clearance'])  # no qualified timing or debt enrollment in this fixture
                bc.cmd_validate(argparse.Namespace(mode='candidate', plan=str(plan_path), force=False), store)
                # A later helper-only amplification must invalidate whole-Build acceptance too.
                helper.write_text('import subprocess,sys\ndef run(): subprocess.run([sys.executable,"-c","pass"],check=True)\n')
                git('add', '.engine/tools/helper.py'); git('commit', '-qm', 'Shared helper amplification')
                bad_head = git('rev-parse', 'HEAD')
                with self.assertRaisesRegex(core.CoordinatorError, 'validation failed'):
                    bc.cmd_validate(argparse.Namespace(mode='candidate', plan=str(plan_path), force=False), store)
                exception = {'id': 'fixture-permission', 'owner': 'fixture operator', 'reason': 'Bounded process witness',
                    'revisit': 'Repair helper', 'issued_at': '2026-09-13T01:00:00Z', 'expires_at': '2026-09-13T02:00:00Z',
                    'case': {'id': 'test_example.C.test_behavior', 'occurrence': 1}, 'resource': 'processes', 'ceiling': 1,
                    'source_commit': bad_head, 'supported_fault': 'Child failure', 'fault_preservation_evidence': 'Fixture witness'}
                exception_path = folder / 'exception.json'; exception_path.write_text(json.dumps(exception))
                with patch.object(bc.moment, 'utc_now', return_value='2026-09-13T01:30:00Z') as clock:
                    bc.cmd_cost_exception(argparse.Namespace(input=str(exception_path), operator_decided=True,
                                                            reason='Explicit fixture operator decision'), store)
                    original = copy.deepcopy(store.read()['cost']['candidate'])
                    with patch.object(bc, '_run_validation', wraps=bc._run_validation) as launch:
                        bc.cmd_validate(argparse.Namespace(mode='candidate', plan=str(plan_path), force=False), store)
                        launch.assert_not_called()
                        # Platform/review facts here are synthetic; the Git source and resource observations are real.
                        ready = store.read()
                        assessment = bc._live_candidate_cost(ready, bad_head)
                        receipt = {'lens': 'technical-integrity', 'commit': bad_head}
                        ready['pr_contract'] = {'body_digest': bc._digest(b'fixture body')}
                        ready['cost']['review'] = {'receipt_key': bc.scoped_agents.receipt_key(receipt),
                            'report': {'findings': [], 'cost_review': {'assessment_digest': cost.digest(assessment),
                                'candidate_identity': assessment['identity'], 'status': 'unavailable',
                                'rationale': 'Timing and baseline remain explicitly unavailable.'}},
                            'disposition': {'assessment_digest': cost.digest(assessment), 'decision': 'accept-with-limitations',
                                            'rationale': 'This is only a bounded lifecycle fixture.'}}
                        pr = {'number': 1, 'state': 'OPEN', 'isDraft': True, 'headRefOid': bad_head,
                              'baseRefOid': base, 'body': 'fixture body', 'mergeable': 'MERGEABLE'}
                        with patch.object(store, 'read', return_value=ready), \
                             patch.object(bc.review, 'retained_receipts', return_value=[('deliverable', receipt)]), \
                             patch.object(bc, '_status', return_value={'phase': 'ready', 'head_commit': bad_head,
                                'required_evidence': [], 'engineering_judgment': []}), \
                             patch.object(bc, '_assert_spec_current'), \
                             patch.object(bc.github, 'pr_state', return_value=pr), \
                             patch.object(bc.github, 'required_check', return_value=('success', None)), \
                             patch.object(bc.github, 'set_ready') as publish:
                            bc.cmd_submit_preview(argparse.Namespace(plan=str(plan_path)), store)
                            clock.return_value = '2026-09-13T02:00:00Z'
                            with self.assertRaisesRegex(core.CoordinatorError, 'unwaived'):
                                bc.cmd_submit_apply(argparse.Namespace(plan=str(plan_path)), store)
                            publish.assert_not_called()
                        with self.assertRaisesRegex(core.CoordinatorError, 'unwaived'):
                            bc.cmd_validate(argparse.Namespace(mode='candidate', plan=str(plan_path), force=False), store)
                        launch.assert_not_called()
                    self.assertEqual(original, store.read()['cost']['candidate'])



import test_build_coordinator as coordinator_fixtures


@cost.declaration({
    "schema_version": "test-cost-contract.v1", "supported_fault": "A valid cost judgment cannot survive native review acceptance and restart",
    "boundary": "process", "boundary_rationale": "Real observed-event and companion ingress; at most one cold shared-fixture origin lookup",
    "fixture_owner": "test_build_coordinator_cost.NativeCostReview", "dependencies": ["jsonschema", "scoped_agents", "reviewer_contracts"],
    "data_reads": [".engine/schemas/*.json", ".engine/policies/model-bindings.json", ".engine/build-protocol.json"],
    "cadence": "pr", "limits": {**cost.zeros(), "schema_decodes": 400, "metaschema_validations": 3, "processes": 1, "git_commands": 1},
    "mutable_state": "Disposable protocol state and synthetic native transcript events; Git and measurement already covered by SerialLifecycle",
    "cache_lifetime": "case", "added_cost_risk": "One cost reviewer, one observed report, no nested inventory", "families": []})
class NativeCostReview(coordinator_fixtures.CoordinatorCase):
    DELIVERABLE_LENSES = ["technical-integrity"]
    packet = coordinator_fixtures.TestReviewAndFindings.packet
    receipt_args = coordinator_fixtures.TestReviewAndFindings.receipt_args

    def setUp(self):
        from unittest.mock import patch
        bc = coordinator_fixtures.bc
        # This fixture tests observed review authority. Real Git and measurement live in SerialLifecycle.
        for seam in (patch.object(bc, "_head", return_value=coordinator_fixtures.HEAD_A),
                     patch.object(bc.review_integrity, "snapshot", return_value={"checkout": "fixture", "origin": None,
                         "branch": "fixture", "head": coordinator_fixtures.HEAD_A, "stash_count": 0, "worktrees": []}),
                     patch.object(bc.ranges, "commits", return_value=[])):
            seam.start(); self.addCleanup(seam.stop)
        super().setUp()

    def test_observed_envelope_survives_acceptance_and_legacy_substitution_is_refused(self):
        import contextlib
        import io
        import json
        from pathlib import Path
        from unittest.mock import patch
        import build_coordinator as bc
        import build_coordinator_review as review
        import reviewer_contracts
        import scoped_agents
        self.seed(); self.approve("thorough"); self.integrate_all()
        fixture_root = Path(scoped_agents.__file__).resolve().parents[2]
        persona = fixture_root / ".claude/agents/engine-qa-review-technical-integrity.md"
        record = self.review_library.read_record(self.review_slug)
        referent = {"plan_id": coordinator_fixtures.PLAN_ID, "revision": 1, "plan_digest": record["current"]["plan_digest"]}
        historical = reviewer_contracts.capture(fixture_root, referent, "thorough", [], self.DELIVERABLE_LENSES,
            instructions="The original frozen array obligation.")
        historical_bytes = json.dumps(historical, sort_keys=True)
        persona.write_text(persona.read_text().replace("reviewer-contract-version: 1", "reviewer-contract-version: 2\nsupports-frozen-cost-predecessor: 1")
                           .replace("output-contract: pre-submission-review-finding.v1", "output-contract: technical-integrity-review.v1"))
        record = self.review_library.read_record(self.review_slug)
        historical_path = Path(self.temp.name) / "historical-packet.txt"
        historical_path.write_text("Original frozen assignment fixture")
        old_assignment = scoped_agents.Store(self.review_library, self.review_slug).register(
            owner=scoped_agents.build_owner(self.store.read()), root="fixture-root", purpose="review", lens="technical-integrity",
            role="engine-qa-review-technical-integrity", packet=historical_path,
            packet_digest=cost.digest("original packet"), review_contract=historical)
        self.assertEqual("pre-submission-review-finding.v1", old_assignment["result_contract"]["id"])
        self.assertEqual(historical_bytes, json.dumps(historical, sort_keys=True))
        frozen = reviewer_contracts.capture(fixture_root,
            {"plan_id": coordinator_fixtures.PLAN_ID, "revision": 1, "plan_digest": record["current"]["plan_digest"]},
            "thorough", [], self.DELIVERABLE_LENSES, instructions="Read the frozen cost assessment.")
        observed, context = cost_example()
        assessment = cost.assess_cost(observed, **context)
        report = {"findings": [], "cost_review": {"assessment_digest": cost.digest(assessment),
            "candidate_identity": assessment["identity"], "status": "unavailable", "rationale": "Timing and descendant coverage remain unavailable."}}
        self.store.mutate(lambda state: state.update(review_contract=frozen, review_contract_format=1,
            cost={"schema_version": "build-cost-evidence.v1", "nodes": {}, "candidate": None, "review": None, "exceptions": []},
            validation={"commit": coordinator_fixtures.HEAD_A, "results": [{"id": "self-test", "commit": coordinator_fixtures.HEAD_A, "passed": True, "summary": "fixture"}]}))
        with patch.object(bc, "ROOT", fixture_root), patch.object(bc, "_live_candidate_cost", return_value=assessment), contextlib.redirect_stdout(io.StringIO()):
            packet = self.packet()
            args = self.receipt_args(packet, "technical-integrity", [])
            args.session = "fixture-root"
            source = Path(self.temp.name) / "report.json"; source.write_text("[]"); args.report = str(source)
            with self.assertRaises(bc.CoordinatorError):
                bc.cmd_review_record(args, self.store)
            source.write_text(json.dumps(report))
            companion, assignment = coordinator_fixtures.observe_review_execution(self.review_library, self.review_slug,
                scoped_agents.build_owner(self.store.read()), "technical-integrity", args.lens_packet_digest, report,
                review_contract=frozen, packet_content=json.dumps(packet))
            bc.cmd_review_record(args, self.store)
        state = self.store.read(); retained = state["cost"]["review"]
        self.assertEqual(report, retained["report"])
        self.assertIsNone(retained["disposition"])
        receipt = state["reviews"]["deliverable"]["receipts"][0]
        reopened = scoped_agents.Store(self.review_library, self.review_slug)
        self.assertTrue(reopened.receipt_verified(receipt, scoped_agents.build_owner(state)))
        self.assertEqual(report, reopened.read()["acceptances"][retained["receipt_key"]]["reports"][assignment["id"]])
        stale = copy.deepcopy(report); stale["cost_review"]["assessment_digest"] = "sha256:" + "f" * 64
        with self.assertRaises(bc.CoordinatorError):
            review.cost_judgment(stale, assessment)
        verdict = review.cost_disposition(report, assessment, {"assessment_digest": cost.digest(assessment),
            "decision": "accept-with-limitations", "rationale": "Retain the explicit coverage limit."})
        self.assertFalse(verdict["cost_clearance"])

if __name__ == "__main__":
    unittest.main()
