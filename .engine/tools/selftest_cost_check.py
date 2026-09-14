#!/usr/bin/env python3
"""Required test-cost wiring and static-inventory checks, with actual resource negative controls.

The full serial run produces resource evidence afterward through ci_gatekeeper.assess-cost.
This pre-run check cannot claim that an observation exists or that performance is qualified.
"""
from __future__ import annotations
import json
from pathlib import Path
import sys
import selftest_cost as cost


# Reviewed enforcement shapes: changing a consumer requires updating this checked contract.
REQUIRED_STEPS = {'cost': {'name': 'Assess observed test cost',
          'id': 'cost',
          'if': "steps.gate.outputs.mode != 'reuse' && steps.gate.outputs.mode != 'project-only'",
          'env': {'GITHUB_TOKEN': '${{ secrets.GITHUB_TOKEN }}', 'ENGINE_TEST_COST_APPROVED_EXCEPTIONS': '${{ vars.ENGINE_TEST_COST_APPROVED_EXCEPTIONS }}'},
          'run': 'mkdir -p "$RUNNER_TEMP/engine-ci-proof"\n'
                 'uv run --directory .engine --frozen -- python tools/ci_gatekeeper.py assess-cost '
                 '--cost-run "$RUNNER_TEMP/selftest-cost.json" --outcomes '
                 '"$RUNNER_TEMP/selftest-results.json" --performance '
                 '"$RUNNER_TEMP/selftest-performance.json" --out '
                 '"$RUNNER_TEMP/engine-ci-proof/cost-evidence.json"\n'},
 'Write the receipt': {'name': 'Write the receipt',
                       'if': "steps.gate.outputs.mode != 'reuse'",
                       'env': {'GITHUB_REPOSITORY': '${{ github.repository }}',
                               'ENGINE_CI_MODE': '${{ steps.gate.outputs.mode }}',
                               'ENGINE_TEST_COST_APPROVED_EXCEPTIONS': '${{ '
                                                                       'vars.ENGINE_TEST_COST_APPROVED_EXCEPTIONS '
                                                                       '}}'},
                       'run': 'cost_args=()\n'
                              'if [ "$ENGINE_CI_MODE" = full ]; then\n'
                              '  cost_args=(--cost-file "$RUNNER_TEMP/engine-ci-proof/cost-evidence.json")\n'
                              'fi\n'
                              'uv run --directory .engine --frozen -- python tools/ci_gatekeeper.py '
                              'emit-receipt --out "$RUNNER_TEMP/engine-ci-proof/receipt.json" '
                              '"${cost_args[@]}"\n'},
 'Upload the receipt': {'name': 'Upload the receipt',
                        'if': "steps.gate.outputs.mode != 'reuse' && (github.event_name == 'pull_request' || github.event_name == 'push')",
                        'uses': 'actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a',
                        'with': {'name': 'engine-ci-receipt',
                                 'path': '${{ runner.temp }}/engine-ci-proof/receipt.json\n'
                                         '${{ runner.temp }}/engine-ci-proof/cost-evidence.json\n',
                                 'retention-days': 30,
                                 'if-no-files-found': 'error',
                                 'overwrite': True}},
 'Refuse a run in which no arm did any work': {'name': 'Refuse a run in which no arm did any work',
                                               'env': {'ENGINE_CI_FULL_RAN': '${{ steps.selftests.outcome }}',
                                                       'ENGINE_CI_REUSE_RAN': '${{ steps.metadata.outcome }}',
                                                       'ENGINE_CI_PROJECT_ONLY_RAN': '${{ '
                                                                                     'steps.project.outcome '
                                                                                     '}}',
                                                       'ENGINE_TEST_COST_APPROVED_EXCEPTIONS': '${{ '
                                                                                               'vars.ENGINE_TEST_COST_APPROVED_EXCEPTIONS '
                                                                                               '}}'},
                                               'run': 'uv run --directory .engine --frozen -- python '
                                                      'tools/ci_gatekeeper.py assert-ran --cost-dir '
                                                      '"$RUNNER_TEMP/engine-ci-proof"'}}

def check(root):
    import yaml
    root = Path(root)
    failures = []
    def require(condition, message):
        if not condition:
            failures.append(message)
    workflow = yaml.safe_load((root / '.github/workflows/engine-ci.yml').read_text())
    job = workflow['jobs']['engine-ci']; steps = job['steps']
    by_id = {s.get('id', s.get('name')): s for s in steps}
    require(not workflow.get('defaults') and not job.get('defaults'), 'enforcement shell defaults must not be overridden')
    require(not workflow.get('env') and not job.get('env'), 'enforcement environment must remain step-scoped')
    for name, expected in REQUIRED_STEPS.items():
        require(by_id.get(name) == expected, 'required cost consumer shape changed: '+name)
    full = "steps.gate.outputs.mode != 'reuse' && steps.gate.outputs.mode != 'project-only'"
    measurement, assessment = by_id['selftests'], by_id['cost']
    emitter, upload = by_id['Write the receipt'], by_id['Upload the receipt']
    terminal = steps[-1]
    require('--cost-path "$RUNNER_TEMP/selftest-cost.json"' in measurement['run'], 'serial observation is missing')
    require('--changed-from' not in measurement['run'] and "--pattern 'test_*.py'" in measurement['run'], 'full final inventory was narrowed')
    require(measurement.get('if') == assessment.get('if') == full, 'cost and correctness must share the full arm')
    require(not measurement.get('continue-on-error') and not assessment.get('continue-on-error'), 'cost and correctness must block')
    require('tools/ci_gatekeeper.py assess-cost' in assessment['run'], 'common assessment consumer is missing')
    for argument in ('--cost-run "$RUNNER_TEMP/selftest-cost.json"', '--outcomes "$RUNNER_TEMP/selftest-results.json"',
                     '--performance "$RUNNER_TEMP/selftest-performance.json"', '--out "$RUNNER_TEMP/engine-ci-proof/cost-evidence.json"'):
        require(argument in assessment['run'], 'assessment acquisition differs: '+argument)
    require(steps.index(measurement) < steps.index(assessment) < steps.index(emitter) < steps.index(upload) < len(steps)-1,
            'observation, assessment, receipt and terminal consumption must be ordered')
    permission = '${{ vars.ENGINE_TEST_COST_APPROVED_EXCEPTIONS }}'
    for step in (by_id['gate'], assessment, emitter, terminal):
        require(step.get('env', {}).get('ENGINE_TEST_COST_APPROVED_EXCEPTIONS') == permission,
                'live maintainer permission must be scoped to each evidence consumer')
    require('ENGINE_TEST_COST_APPROVED_EXCEPTIONS' not in job.get('env', {}) and
            'ENGINE_TEST_COST_APPROVED_EXCEPTIONS' not in measurement.get('env', {}), 'permission must not become test-owned job state')
    require(by_id['gate'].get('env', {}).get('ENGINE_CI_COST_REUSE_DIR') == '${{ runner.temp }}/engine-ci-proof', 'reuse must retain its original proof for completion')
    require('--cost-file "$RUNNER_TEMP/engine-ci-proof/cost-evidence.json"' in emitter['run'], 'full receipt must consume cost evidence')
    require('--out "$RUNNER_TEMP/engine-ci-proof/receipt.json"' in emitter['run'], 'receipt location differs from terminal consumer')
    require(emitter.get('if') == "steps.gate.outputs.mode != 'reuse'", 'local full proof must also exist for default-branch completion')
    for name in ('receipt.json', 'cost-evidence.json'):
        require('${{ runner.temp }}/engine-ci-proof/'+name in upload['with']['path'], 'merge proof upload is incomplete')
    require('tools/ci_gatekeeper.py assert-ran --cost-dir "$RUNNER_TEMP/engine-ci-proof"' in terminal['run']
            and 'if' not in terminal and not terminal.get('continue-on-error'), 'completion must reconsume live cost permission')
    static_path = root / '.engine/policies/test-cost-legacy-static.json'
    legacy = json.loads(static_path.read_text()) if static_path.exists() else {}
    census = cost.static_census({p.relative_to(root).as_posix(): p.read_text()
        for p in (root / '.engine/tools').rglob('test_*.py')}, '0'*40)
    failures += cost.duplicate_findings(census, legacy)
    return failures


def main():
    import validate
    control = validate.env_override_path('ENGINE_TEST_COST_CONTROL_PATH')
    if control:
        from demo_test_cost_contracts import demonstrate
        scenarios = json.loads(Path(control).read_text())['scenarios']
        outcomes = [demonstrate(scenario) for scenario in scenarios]
        found = [validate.finding('hard', 'test-cost violation '+o['scenario']+': '+ '; '.join(o['violations']))
                 for o in outcomes if o['violations']]
        if len(outcomes) == 8 and all(o['passed'] for o in outcomes):
            found.append(validate.finding('hard', 'all 8 resource/inventory controls rejected; repaired controls pass'))
    else:
        try:
            found = [validate.finding('hard', 'test-cost: '+message) for message in check(validate.ROOT)]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            found = [validate.finding('hard', 'test-cost input is unavailable: '+str(exc))]
    print(json.dumps(found))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
