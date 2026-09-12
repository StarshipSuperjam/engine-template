"""Read-only test-performance reports and bounded CI summaries; never merge receipts."""
from __future__ import annotations

import argparse
from collections import Counter
import html
import hashlib
import io
import json
import os
from pathlib import Path
import re
import statistics
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import zipfile

import selftest_results as records
import moment

TARGET_SECONDS = 900
CONCERN_SECONDS = 1200
RESULTS_PREFIX = "engine-selftest-results"
PERFORMANCE_PREFIX = "engine-selftest-performance"
MAX_PAGES = 100
MAX_ATTEMPTS = 100
FULL_STEP = "Run the self-tests (the checker-of-checkers)"
VALIDATOR_STEP = "Run the seed validator (CI suite)"
PROJECT_STEP = "Run the seed validator alone (project-only change set)"
REUSE_STEP = "Re-check what an unchanged tree cannot settle"


class APIError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlparse(newurl).scheme != "https":
            raise ValueError("non-HTTPS artifact redirect")
        target = super().redirect_request(req, fp, code, msg, headers, newurl)
        if urllib.parse.urlparse(req.full_url).netloc != urllib.parse.urlparse(newurl).netloc:
            target.remove_header("Authorization")
        return target


class GitHub:
    """GET-only bounded adapter. Credentials never enter reports, argv or redirect hosts."""
    def __init__(self, repository):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("repository must be OWNER/REPO")
        self.repository = repository
        self.prefix = f"/repos/{repository}"
        self.token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if not self.token:
            try:
                token = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=15)
                if token.returncode == 0:
                    self.token = token.stdout.strip()
            except (OSError,subprocess.SubprocessError):
                pass  # Public reads remain possible; missing private access becomes explicit API failure.
        self.opener = urllib.request.build_opener(_Redirect())

    def raw(self, path):
        if not path.startswith(self.prefix + "/"):
            raise ValueError("API path is outside the selected repository")
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        try:
            with self.opener.open(urllib.request.Request("https://api.github.com"+path, headers=headers), timeout=30) as response:
                raw = response.read(records.MAX_BYTES + 1)
        except urllib.error.HTTPError as exc:
            # Record only status and bounded server message, never headers or credential-bearing URLs.
            try:
                message = json.loads(exc.read(8192)).get("message", "API request failed")
            except (ValueError, AttributeError):
                message = "API request failed"
            raise APIError(exc.code, records.text(message)) from None
        if len(raw) > records.MAX_BYTES:
            raise ValueError("API response exceeds byte limit")
        return raw

    def get(self, path):
        return json.loads(self.raw(self.prefix + path))

    def pages(self, path, key=None):
        rows = []
        for page in range(1, MAX_PAGES + 1):
            data = self.get(path + ("&" if "?" in path else "?") + f"per_page=100&page={page}")
            batch = data[key] if key else data
            if not isinstance(batch, list):
                raise ValueError("API page is not an array")
            rows.extend(batch)
            if len(batch) < 100:
                return rows
        raise ValueError("API pagination limit reached; coverage is incomplete")

    def artifact(self, artifact, filename):
        if artifact.get("expired") or type(artifact.get("size_in_bytes")) is not int or artifact["size_in_bytes"] > records.MAX_BYTES:
            raise ValueError("artifact unavailable or oversized")
        raw = self.raw(self.prefix + f"/actions/artifacts/{int(artifact['id'])}/zip")
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = archive.infolist()
            if len(members) != 1 or members[0].filename != filename or members[0].file_size > records.MAX_BYTES:
                raise ValueError("artifact must contain exactly its bounded named JSON file")
            with archive.open(members[0]) as stream:
                data = stream.read(records.MAX_BYTES+1)
        if len(data) > records.MAX_BYTES:
            raise ValueError("expanded artifact exceeds byte limit")
        return json.loads(data), hashlib.sha256(data).hexdigest()


def required_contexts(api, base):
    """Snapshot both policy owners; 404 is absence only for an explicit unprotected response."""
    branch = urllib.parse.quote(base, safe="")
    rules = api.pages(f"/rules/branches/{branch}")
    try:
        classic = api.get(f"/branches/{branch}/protection/required_status_checks")
    except APIError as exc:
        if exc.status == 404 and str(exc) == "Branch not protected":
            classic = None
        else:
            raise
    contexts = {}
    def add(name, app=None):
        if not isinstance(name, str) or not name:
            raise ValueError("required context name absent")
        if name in contexts and contexts[name] not in (None, app) and app is not None:
            raise ValueError("conflicting required app bindings")
        contexts[name] = app if app is not None else contexts.get(name)
    for rule in rules:
        if rule["type"] == "required_status_checks":
            for check in rule["parameters"]["required_status_checks"]:
                add(check["context"], check.get("integration_id"))
        elif rule["type"] in {"workflows", "required_workflows"}:
            raise ValueError("required workflow policy cannot be reduced to named check contexts")
    if classic:
        for check in classic.get("checks", []):
            add(check["context"], check.get("app_id"))
        for name in classic.get("contexts", []):
            add(name)
    if not contexts:
        raise ValueError("no required CI contexts could be established")
    return contexts, {"base": base, "rules": rules, "classic": classic,
                      "observed_at": moment.utc_now(), "historical_policy": False}


def timestamp(value):
    result = moment.epoch(value)
    if result is None:
        raise ValueError("required timing field is absent or invalid")
    return result


def job_fact(job):
    if job["status"] != "completed":
        raise ValueError("workflow job is not completed")
    start, end = timestamp(job["started_at"]), timestamp(job["completed_at"])
    created = timestamp(job["created_at"])
    if not created <= start <= end:
        raise ValueError("job timestamps are out of order")
    steps = []
    for step in job["steps"]:
        a, b = timestamp(step["started_at"]), timestamp(step["completed_at"])
        if not start <= a <= b <= end:
            raise ValueError("step timestamps are outside their job")
        steps.append({"name": records.text(step["name"]), "conclusion": step["conclusion"],
                      "seconds": b-a, "started_at": step["started_at"], "completed_at": step["completed_at"]})
    return {"id": job["id"], "name": records.text(job["name"]), "conclusion": job["conclusion"],
            "created_at": job["created_at"], "started_at": job["started_at"], "completed_at": job["completed_at"],
            "seconds": end-start, "wait_seconds": start-created, "labels": job.get("labels", []), "steps": steps}


def association(value):
    try:
        context, rest = value.split("=", 1)
        run, attempt, workflow = rest.split(":", 2)
        if not context or len(context) > 256 or not re.fullmatch(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml", workflow):
            raise ValueError()
        run, attempt = int(run), int(attempt)
        if run < 1 or not 1 <= attempt <= MAX_ATTEMPTS:
            raise ValueError()
        return context, {"run": run, "attempt": attempt, "workflow": workflow}
    except ValueError:
        raise ValueError("context must be NAME=RUN:ATTEMPT:.github/workflows/FILE.yml") from None


def run_route(jobs, event):
    if event == "push":
        return "main-push"
    names = {step["name"] for job in jobs for step in job["steps"] if step["conclusion"] != "skipped"}
    routes = []
    if FULL_STEP in names or VALIDATOR_STEP in names:
        routes.append("full-pr")
    if PROJECT_STEP in names:
        routes.append("project-only")
    if REUSE_STEP in names:
        routes.append("metadata-reuse")
    if len(routes) != 1:
        raise ValueError("cannot determine a unique substantive run route")
    return routes[0]


def completed_report(api, head, primary_run, primary_attempt, contexts=None, base=None):
    """Explicit identities only. Missing facts remain visible, never latest-success substitution."""
    if not re.fullmatch(r"[0-9a-f]{40}", head) or primary_run < 1 or not 1 <= primary_attempt <= MAX_ATTEMPTS:
        raise ValueError("head must be a full SHA and run/attempt positive bounded integers")
    requested = {"engine-ci": {"run": primary_run, "attempt": primary_attempt, "workflow": ".github/workflows/engine-ci.yml"}}
    for name, value in contexts or []:
        if name in requested:
            raise ValueError("duplicate context association")
        requested[name] = value
    report = {"schema_version": "ci-test-performance.v1", "repository": api.repository, "head": head,
              "observed_at": moment.utc_now(), "complete": False, "timing_complete": False,
              "requirements": None, "associations": requested, "attempts": [], "observations": [],
              "route": None, "metrics": None, "environment": None, "cases": None,
              "issues": [], "target_seconds": TARGET_SECONDS, "concern_seconds": CONCERN_SECONDS,
              "manual_wait_seconds": None, "timing_note": "Job wait is observed created-to-start delay; manual approval time is unknown. Active union does not reconstruct a dependency DAG."}
    try:
        primary = api.get(f"/actions/runs/{primary_run}/attempts/{primary_attempt}")
        if primary['event']=='push':
            report['route']='main-push'
        if base is None:
            bases = {pr["base"]["ref"] for pr in primary.get("pull_requests", []) if pr.get("head", {}).get("sha") == head}
            if len(bases) == 1:
                base = bases.pop()
            elif primary["event"] == "push":
                base = primary["head_branch"]
            else:
                raise ValueError("PR base is ambiguous; supply --base explicitly")
        required, policy = required_contexts(api, base)
        report["requirements"] = policy
        if primary['event']=='push':
            # Main push is a reference workflow class, never qualification of a PR's required path.
            policy['applicability']='main-push reference workflows explicitly supplied; no PR-path credit'
            if not set(requested)<=set(required):
                raise ValueError('main-push association is outside required-context policy')
            required={name:required[name] for name in requested}
        if set(requested) != set(required):
            raise ValueError("explicit associations must match every required context exactly")
        checks = api.pages(f"/commits/{head}/check-runs?filter=all", "check_runs")
        cache = {}
        selected_jobs = []
        for context, target in requested.items():
            for attempt in range(1, target["attempt"]+1):
                key = target["run"], attempt
                if key not in cache:
                    run = primary if key == (primary_run,primary_attempt) else api.get(f"/actions/runs/{key[0]}/attempts/{attempt}")
                    if run["id"] != key[0] or run["run_attempt"] != attempt or run["path"] != target["workflow"] or run["status"] != "completed":
                        raise ValueError("workflow identity, attempt, path or completion mismatches association")
                    if run["event"] != "pull_request_target" and run["head_sha"] != head:
                        raise ValueError("workflow run does not belong to requested head")
                    jobs = api.pages(f"/actions/runs/{key[0]}/attempts/{attempt}/jobs", "jobs")
                    if not jobs or len({j['id'] for j in jobs}) != len(jobs):
                        raise ValueError("missing or duplicate jobs")
                    for job in jobs:
                        if job["run_id"] != key[0] or job["run_attempt"] != attempt:
                            raise ValueError("job belongs to another run or attempt")
                    fact = {"run": key[0], "attempt": attempt, "workflow": run["path"], "event": run["event"],
                            "workflow_head": run["head_sha"], "created_at": run["created_at"],
                            "run_started_at": run["run_started_at"], "updated_at": run["updated_at"],
                            "conclusion": run["conclusion"], "jobs": [job_fact(j) for j in jobs]}
                    fact["runner_minutes"] = sum(j["seconds"] for j in fact["jobs"])/60
                    last = max(timestamp(j["completed_at"]) for j in jobs)
                    fact["observed_finalization_seconds"] = max(0, timestamp(run["updated_at"])-last)
                    report["attempts"].append(fact)
                    cache[key] = run, jobs, fact
                run, jobs, fact = cache[key]
                if run['path'] != target['workflow']:
                    raise ValueError('cached workflow identity mismatches association')
                matches = [job for job in jobs if job["name"] == context]
                if len(matches) != 1:
                    raise ValueError("context does not identify exactly one attempt job")
                job = matches[0]
                linked = [check for check in checks if check["name"] == context and check["head_sha"] == head
                          and check.get("url") == job["check_run_url"]
                          and check.get("check_suite", {}).get("id") == run["check_suite_id"]]
                if len(linked) != 1 or linked[0].get("app", {}).get("slug") != "github-actions":
                    raise ValueError("missing or ambiguous PR-head check association")
                check = linked[0]
                if required[context] is not None and check["app"]["id"] != required[context]:
                    raise ValueError("required app binding does not match the check")
                if check["conclusion"] != job["conclusion"] or check["status"] != "completed":
                    raise ValueError("check and attempt job outcomes disagree")
                if attempt == target["attempt"]:
                    selected_jobs.append((run, job))
        starts = [timestamp(run["created_at"]) for run, job in selected_jobs]
        last = max(timestamp(job["completed_at"]) for run, job in selected_jobs)
        active = [(timestamp(j["started_at"]), timestamp(j["completed_at"])) for _,j in selected_jobs]
        all_required_jobs=[j for a in report['attempts'] for j in a['jobs'] if j['name'] in required]
        all_active=[(timestamp(j['started_at']),timestamp(j['completed_at'])) for j in all_required_jobs]
        waits = [(timestamp(j["created_at"]), timestamp(j["started_at"])) for j in all_required_jobs]
        active_union = records.interval_union(active)
        queue_only = records.interval_union(all_active+waits)-records.interval_union(all_active)
        report["metrics"] = {"elapsed_seconds": last-min(starts), "active_union_seconds": active_union,
                             "queue_only_seconds": queue_only, "queue_excluded_seconds": last-min(starts)-queue_only,
                             "runner_minutes": sum((b-a) for a,b in active)/60,
                             "cumulative_runner_minutes": sum(a["runner_minutes"] for a in report["attempts"]),
                             "attempt_count": len(report["attempts"])}
        report["timing_complete"] = True
        report["route"] = run_route(cache[(primary_run,primary_attempt)][1], primary["event"])
        artifacts = api.pages(f"/actions/runs/{primary_run}/artifacts", "artifacts")
        if report["route"] not in {"metadata-reuse", "project-only"}:
            documents = {}
            for prefix, filename, version in [(RESULTS_PREFIX,"selftest-results.json","selftest-results.v1"),
                                               (PERFORMANCE_PREFIX,"selftest-performance.json","selftest-performance.v1")]:
                name = f"{prefix}-{primary_run}-{primary_attempt}"
                found = [a for a in artifacts if a.get("name") == name and not a.get("expired")]
                if len(found) != 1:
                    raise ValueError("attempt-specific observation artifact missing or ambiguous")
                document, digest = api.artifact(found[0], filename)
                records.validate_shape(document,version)
                if document["ci"]["run_id"] != str(primary_run) or document["ci"]["run_attempt"] != str(primary_attempt):
                    raise ValueError("observation artifact run/attempt mismatches")
                documents[version] = document
                report["observations"].append({"name": name, "artifact_id": found[0]["id"], "sha256": digest})
            outcomes, metrics = documents["selftest-results.v1"], documents["selftest-performance.v1"]
            records.validate(outcomes)
            if outcomes["source"] != metrics["source"] or outcomes["ci"] != metrics["ci"]:
                raise ValueError("outcome and timing source identities differ")
            if outcomes['scope'] != 'full' or metrics['scope'] != 'full' or outcomes['invocation'] != metrics['invocation'] or metrics['invocation']['pattern'] != 'test_*.py':
                raise ValueError("full CI observations describe a different selection")
            checkout = outcomes["ci"]["head"]
            if not isinstance(checkout,str) or not re.fullmatch(r"[0-9a-f]{40}", checkout):
                raise ValueError("observation checkout identity absent")
            commit = api.get(f"/commits/{checkout}")
            if commit["sha"] != checkout or commit["commit"]["tree"]["sha"] != outcomes["source"]["tree"] or outcomes["source"]["worktree_dirty"] is not False:
                raise ValueError("observation tree does not match its clean checkout")
            if checkout != head and (primary["event"] != "pull_request" or len(commit["parents"]) != 2 or commit["parents"][1]["sha"] != head):
                raise ValueError("checkout is neither requested head nor its PR merge commit")
            durations = {(r["id"],r["occurrence"]):r["seconds"] for r in metrics["cases"]}
            if len(durations) != len(metrics["cases"]) or set(durations) != {(r["id"],r["occurrence"]) for r in outcomes["cases"]}:
                raise ValueError("timing/outcome inventories differ")
            report["cases"] = [{"id":r["id"],"occurrence":r["occurrence"],"outcome":r["outcome"],
                                "seconds":durations[(r["id"],r["occurrence"])]} for r in outcomes["cases"]]
            report["environment"] = {**metrics["environment"], "pattern":metrics["invocation"]["pattern"],
                                      "scope":outcomes["scope"], "runner_labels":cache[(primary_run,primary_attempt)][2]["jobs"][0]["labels"]}
            if not outcomes["complete"]:
                raise ValueError("test outcomes are incomplete")
        report["complete"] = True
    except (APIError, OSError, ValueError, KeyError, TypeError, RecursionError, zipfile.BadZipFile, urllib.error.URLError) as exc:
        report["issues"].append(records.text(str(exc)))
    return report


def compare_reports(baseline, candidate):
    """A comparison is evidence, not a wall-clock gate. Unknown dimensions cannot qualify."""
    for report in (baseline,candidate):
        records.validate_shape(report,'ci-test-performance.v1')
    reasons=[]
    for field in ('repository','route'):
        if baseline[field] != candidate[field]:
            reasons.append(field+' differs')
    if not baseline['complete'] or not candidate['complete']:
        reasons.append('one or both reports are incomplete')
    left,right=baseline['environment'],candidate['environment']
    dimensions=['os','release','architecture','python','cpu_count','runner_os','runner_arch','uv',
                'memory_bytes','worktree_count','cache','pattern','scope','runner_labels']
    for key in dimensions:
        a,b=(left or {}).get(key),(right or {}).get(key)
        if a in (None,'unknown') or b in (None,'unknown'):
            reasons.append(key+' unavailable')
        elif a != b:
            reasons.append(key+' differs')
    raw={'baseline':baseline['metrics'],'candidate':candidate['metrics']}
    deltas={}
    if baseline['metrics'] and candidate['metrics']:
        for name in ('elapsed_seconds','queue_excluded_seconds','active_union_seconds','runner_minutes','cumulative_runner_minutes'):
            deltas[name]=candidate['metrics'][name]-baseline['metrics'][name]
    cases=None
    if baseline['cases'] is not None and candidate['cases'] is not None:
        def keyed(rows):
            values={(r['id'],r['occurrence']):r for r in rows}
            if len(values)!=len(rows):raise ValueError('duplicate case identities in comparison')
            return values
        a,b=keyed(baseline['cases']),keyed(candidate['cases'])
        common=[]
        for key in sorted(a.keys() & b.keys()):
            x,y=a[key],b[key]
            common.append({'id':key[0],'occurrence':key[1], 'baseline_outcome':x['outcome'],
                           'candidate_outcome':y['outcome'],'baseline_seconds':x['seconds'],
                           'candidate_seconds':y['seconds'],
                           'delta_seconds':None if x['seconds'] is None or y['seconds'] is None else y['seconds']-x['seconds']})
        cases={'common':common,'added':[b[k] for k in sorted(b.keys()-a.keys())],
               'removed':[a[k] for k in sorted(a.keys()-b.keys())],
               'note':'Common identities do not imply unchanged cost: shared code and fixtures can change their work.'}
    return {'schema_version':'ci-test-performance-comparison.v1','qualified':not reasons,'reasons':reasons,
            'raw_samples':raw,'deltas':deltas,'cases':cases,
            'candidate_concern':bool(candidate['metrics'] and candidate['metrics']['elapsed_seconds']>=CONCERN_SECONDS),
            'target_seconds':TARGET_SECONDS,'concern_seconds':CONCERN_SECONDS,
            'note':'One pair cannot qualify the program target; retain failed/retried samples. No p90 claim is made.'}


def sample_summary(reports):
    """Show samples before statistics; three samples earn no tail-percentile claim."""
    samples=[r['metrics']['elapsed_seconds'] if r['metrics'] else None for r in reports]
    values=[v for v in samples if v is not None]
    return {'samples':samples,'sample_count':len(samples),'observed_count':len(values),
            'p50_seconds':statistics.median(values) if values else None,
            'p90_seconds':statistics.quantiles(values,n=10,method='inclusive')[8] if len(values)>=20 else None,
            'concerns':[i for i,v in enumerate(samples) if v is not None and v>=CONCERN_SECONDS],
            'note':'Descriptive only; mixed routes, environments or incomplete reports do not establish qualification.'}


def observed_summary(results_path, performance_path):
    lines = ["## Observed self-test interval", "",
             "This interval covers the serial self-test launcher, not the complete required PR CI path.", ""]
    try:
        result = records.read(results_path)
        complete, passed = records.validate(result)
        counts = Counter(row["outcome"] for row in result["cases"])
        lines.append(f"Outcomes: {'complete' if complete else 'incomplete'}; {'passed' if passed else 'not passed'}.")
        lines.append(f"Selected: {len(result['cases'])}; observed starts: {result['executed_count']}.")
        lines.append("; ".join(f"{name}: {count}" for name,count in sorted(counts.items())))
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        lines.append("Outcomes: unknown (missing or invalid observation). The self-test step owns the verdict.")
    try:
        metrics = records.read(performance_path)
        records.validate_shape(metrics, "selftest-performance.v1")
        seconds = metrics["parent_seconds"]
        if seconds is None:
            raise ValueError("parent interval missing")
        lines.extend(["", f"Launcher elapsed: {seconds:.3f} seconds; collection: {metrics['collection_seconds']:.3f} seconds.",
                      "Case and fixture spans are inclusive; nested test runs are parent cost.", "", "Slowest observed cases:"])
        for row in sorted(metrics["cases"], key=lambda r: -(r["seconds"] or 0))[:20]:
            duration = "unknown" if row["seconds"] is None else f"{row['seconds']:.6f}s"
            # HTML-escaped inside a code element; never interpret test names as Markdown or workflow commands.
            label = html.escape(records.text(row["id"])).replace("\n", " ").replace("\r", " ")
            lines.append(f"- <code>{label}</code> occurrence {row['occurrence']}: {duration}")
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        lines.extend(["", "Timing: unknown (missing, incomplete or invalid optional observations)."])
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    publish = subs.add_parser("publish", help="append bounded self-test observations to the job summary")
    publish.add_argument("--results", required=True)
    publish.add_argument("--performance", required=True)
    report = subs.add_parser('report-ci',help='read completed required CI for explicit head/run/attempt associations')
    report.add_argument('--repository',required=True)
    report.add_argument('--head',required=True)
    report.add_argument('--run',required=True,type=int)
    report.add_argument('--attempt',required=True,type=int)
    report.add_argument('--base')
    report.add_argument('--context',action='append',default=[],metavar='NAME=RUN:ATTEMPT:WORKFLOW')
    report.add_argument('--output')
    compare=subs.add_parser('compare',help='compare explicit baseline/candidate report files')
    compare.add_argument('--baseline',required=True)
    compare.add_argument('--candidate',required=True)
    compare.add_argument('--output')
    samples=subs.add_parser('samples',help='show every supplied report sample and descriptive statistics')
    samples.add_argument('reports',nargs='+')
    samples.add_argument('--output')
    args = parser.parse_args(argv)
    if args.command != 'publish':
        try:
            if args.command=='report-ci':
                value=completed_report(GitHub(args.repository),args.head,args.run,args.attempt,
                                       [association(s) for s in args.context],args.base)
                code=0 if value['complete'] else 1
            elif args.command=='compare':
                value=compare_reports(records.read(args.baseline),records.read(args.candidate));code=0
            else:
                reports=[records.read(path) for path in args.reports]
                for item in reports:records.validate_shape(item,'ci-test-performance.v1')
                value=sample_summary(reports);code=0
            if args.output:records.write(args.output,value)
            else:print(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False))
            return code
        except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError) as exc:
            print('Performance report unavailable: '+records.text(str(exc)))
            return 1
    summary = observed_summary(args.results, args.performance)
    destination = os.environ.get("GITHUB_STEP_SUMMARY")
    if destination:
        try:
            with open(destination, "a", encoding="utf-8") as stream:
                stream.write(summary)
        except OSError:
            print("Self-test summary unavailable; original test verdict is unchanged.")
    else:
        print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
