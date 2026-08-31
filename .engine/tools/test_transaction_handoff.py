#!/usr/bin/env python3
"""The pre-mutation state matrix and the two handoff shapes.

Every test here runs against a throwaway git repository, never this checkout: these paths commit and
mutate, and a test that succeeds in doing the wrong thing has then done it for real.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import transaction  # noqa: E402
import transaction_handoff as th  # noqa: E402


def git(root, *args):
    return subprocess.run(["git"] + list(args), cwd=root, capture_output=True, text=True)


class ThrowawayRepo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.email", "test@example.invalid")
        git(self.root, "config", "user.name", "Test")
        os.makedirs(os.path.join(self.root, ".engine", "modules"), exist_ok=True)
        self._write(".engine/modules/seed.json", "{}\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "seed")

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return rel


class TestPreMutationRefusals(ThrowawayRepo):
    def test_a_clean_tree_on_a_named_branch_is_ready(self):
        state = th.refuse_unless_ready([".engine/modules/design-review.json"], root=self.root)
        self.assertEqual(state["branch"], "main")

    def test_uncommitted_work_in_the_transaction_s_own_paths_refuses(self):
        self._write(".engine/modules/design-review.json", "{}\n")
        with self.assertRaises(transaction.TransactionRefused) as caught:
            th.refuse_unless_ready([".engine/modules/design-review.json"], root=self.root)
        self.assertEqual(caught.exception.code, "uncommitted-changes-in-scope")
        self.assertTrue(caught.exception.next_actions)

    def test_unrelated_uncommitted_work_does_not_refuse(self):
        """The operator's own work in progress is their business, not something to sweep in or block on."""
        self._write("README.md", "my own notes\n")
        state = th.refuse_unless_ready([".engine/modules/design-review.json"], root=self.root)
        self.assertIn("README.md", state["dirty_paths"])

    def test_a_detached_head_refuses(self):
        head = git(self.root, "rev-parse", "HEAD").stdout.strip()
        git(self.root, "checkout", "-q", head)
        with self.assertRaises(transaction.TransactionRefused) as caught:
            th.refuse_unless_ready([".engine/modules/x.json"], root=self.root)
        self.assertEqual(caught.exception.code, "detached-head")

    def test_a_transaction_may_not_claim_the_plan_library_or_the_operator_s_memory(self):
        for forbidden in (".engine/plans/x.json", ".engine/memory/y.json"):
            with self.assertRaises(transaction.TransactionRefused) as caught:
                th.refuse_unless_ready([forbidden], root=self.root)
            self.assertEqual(caught.exception.code, "path-not-claimable")

    def test_every_refusal_carries_a_stable_code_and_a_way_forward(self):
        head = git(self.root, "rev-parse", "HEAD").stdout.strip()
        git(self.root, "checkout", "-q", head)
        with self.assertRaises(transaction.TransactionRefused) as caught:
            th.refuse_unless_ready([".engine/modules/x.json"], root=self.root)
        code = caught.exception.code
        self.assertTrue(code.islower() and " " not in code)
        self.assertTrue(caught.exception.next_actions)
        self.assertTrue(caught.exception.explanation)


class TestSelectiveCommit(ThrowawayRepo):
    def test_only_the_declared_paths_are_committed(self):
        """The property that makes the change revertable as a unit."""
        self._write(".engine/modules/design-review.json", '{"id": "design-review"}\n')
        self._write("product-code.py", "print('the operator's own work')\n".replace("'s", "s"))
        result = th.commit_in_tree([".engine/modules/design-review.json"],
                                   "Add the design-review add-on", root=self.root)
        self.assertTrue(result["committed"])
        listed = git(self.root, "show", "--name-only", "--format=", "HEAD").stdout.split()
        self.assertEqual(listed, [".engine/modules/design-review.json"])
        # The operator's unrelated work is still uncommitted, exactly where they left it.
        self.assertIn("product-code.py", git(self.root, "status", "--porcelain").stdout)

    def test_the_commit_is_revertable_as_a_unit(self):
        self._write(".engine/modules/design-review.json", '{"id": "design-review"}\n')
        th.commit_in_tree([".engine/modules/design-review.json"], "Add it", root=self.root)
        self.assertTrue(os.path.exists(os.path.join(self.root, ".engine/modules/design-review.json")))
        git(self.root, "revert", "--no-edit", "HEAD")
        self.assertFalse(os.path.exists(os.path.join(self.root, ".engine/modules/design-review.json")))

    def test_a_deletion_is_staged_too(self):
        target = ".engine/modules/seed.json"
        os.remove(os.path.join(self.root, target))
        result = th.commit_in_tree([target], "Remove the seed module", root=self.root)
        self.assertTrue(result["committed"])
        self.assertNotIn(target, git(self.root, "ls-files").stdout)

    def test_nothing_to_commit_is_reported_rather_than_faked(self):
        result = th.commit_in_tree([".engine/modules/seed.json"], "No change", root=self.root)
        self.assertIsNone(result["committed"])
        self.assertIn("already in place", result["note"])


class TestHandoffShapes(unittest.TestCase):
    def test_an_in_tree_handoff_tells_the_operator_how_to_undo_it(self):
        handoff = th.in_tree_handoff({"committed": "abc1234"}, "Added the design-review add-on.")
        self.assertEqual(handoff["kind"], "in-tree-commit")
        self.assertIn("reverting that commit", handoff["summary"])
        self.assertEqual(handoff["reference"], "abc1234")

    def test_a_pull_request_handoff_says_nothing_changes_until_merge(self):
        handoff = th.pull_request_handoff({"url": "https://example.invalid/pr/1"}, "The update is ready.")
        self.assertEqual(handoff["kind"], "pull-request")
        self.assertIn("until you merge", handoff["summary"])


class TestStaysOnTheArrivalFloor(unittest.TestCase):
    def test_standard_library_only_with_the_future_import(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "transaction_handoff.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("from __future__ import annotations", source)
        for third_party in ("jsonschema", "yaml", "requests"):
            self.assertNotIn("import {0}".format(third_party), source)
        self.assertNotIn("import tomllib", source)


class TestTheOpenerStagesSelectively(unittest.TestCase):
    """The opener CAN stage selectively when given paths.

    No production caller passes them today: upgrade and whole-engine removal deliberately stage the whole
    tree because they refuse unless it is clean, so everything changed IS their change. The operations
    that run against a possibly-dirty tree — module add and remove — do not use this path at all; they
    commit their declared set through commit_in_tree. This pins the capability, not a claim that the
    pull-request path is selective today."""

    def test_module_manager_stages_declared_paths_when_given_them(self):
        """BEHAVIOURAL. This was a whitespace-exact assertion on the opener's source text -- the third
        instance of a pattern this build twice claimed to have retired, and it proved nothing about what
        the opener does. It now captures the git commands actually issued."""
        import module_manager
        staged = []

        fake_subprocess = mock.Mock()
        fake_subprocess.CalledProcessError = subprocess.CalledProcessError
        fake_subprocess.run.side_effect = lambda step, **kw: staged.append(list(step))
        fake_client = mock.Mock()
        fake_client.request.side_effect = RuntimeError("stop before the POST")
        fake_boot = mock.Mock()
        fake_boot.repo_slug.return_value = "o/r"
        fake_boot.gh_token.return_value = "t"
        fake_identity = mock.Mock()
        fake_identity.resolve_default_branch.return_value = "main"

        def run(paths):
            del staged[:]
            with mock.patch.dict(sys.modules, {"subprocess": fake_subprocess, "github_client": fake_client,
                                               "boot": fake_boot, "repo_identity": fake_identity}):
                try:
                    module_manager._open_upgrade_pr("b", "t", "body", paths=paths)
                except Exception:   # noqa: BLE001 — the POST stub ends it; the git steps are the point
                    pass
            return [step for step in staged if len(step) > 1 and step[1] == "add"]

        self.assertEqual(run(["a.py", "b.py"]), [["git", "add", "--", "a.py", "b.py"]])
        self.assertEqual(run(None), [["git", "add", "-A"]])
class TestACredentialNeverReachesAPullRequestBody(unittest.TestCase):
    """The seam obligation: a credential-shaped value seeded into a plan input is absent from the body.

    The narrower envelope test (`a credential in a plan never reaches the canonical form`) proves a
    DIFFERENT property — that a credential does not move the consent-handle hash. Nothing there says the
    value stays out of the pull-request text an operator publishes. This covers that.
    """

    def test_the_exact_live_token_cannot_survive_even_in_an_unknown_format(self):
        body = "the update pushed using xyzzy-not-a-known-shape-42 as its credential"
        redacted = th.redact_credential_values(body, "xyzzy-not-a-known-shape-42")
        self.assertNotIn("xyzzy-not-a-known-shape-42", redacted)
        self.assertIn(th.REDACTED, redacted)

    def test_documented_credential_shapes_are_stripped_without_a_live_token_to_compare_against(self):
        for seeded in ("ghp_16C7e42F292c69", "github_pat_11ABCDE_xYz09", "gho_abc123", "ghs_deadbeef"):
            redacted = th.redact_credential_values("Scope: applied {0} here.".format(seeded))
            self.assertNotIn(seeded, redacted, seeded)

    def test_a_token_carried_in_a_remote_url_is_stripped_too(self):
        redacted = th.redact_credential_values("failed pushing to https://ghp_tok@github.com/o/r")
        self.assertNotIn("ghp_tok", redacted)
        self.assertIn("github.com/o/r", redacted)   # the diagnosis survives; only the secret goes

    def test_a_bare_prefix_earlier_in_the_text_does_not_shield_a_real_token_later(self):
        """Ordering was the whole bug: the scan used to abandon a prefix entirely on its first harmless
        occurrence, so a real credential further down survived. Every other seeded test here uses one
        token in isolation, which is precisely why none of them could catch it."""
        out = th.redact_credential_values("tokens look like ghp_ and here is ghp_AAAABBBBCCCCDDDD")
        self.assertNotIn("ghp_AAAABBBBCCCCDDDD", out)

    def test_several_real_tokens_in_one_body_are_all_stripped(self):
        out = th.redact_credential_values("ghp_FIRSTONE1 then ghp_ bare then ghp_SECONDONE2")
        self.assertNotIn("ghp_FIRSTONE1", out)
        self.assertNotIn("ghp_SECONDONE2", out)

    def test_ordinary_body_prose_is_left_exactly_alone(self):
        """A scrubber that mangles legitimate text costs something real on every merge."""
        for kept in ("Moves this engine to 1.2.0.",
                     "Retires a capability you have now: design-review.",
                     "The ghp_ prefix is what a GitHub token starts with.",
                     "sha256:" + "a" * 64):
            self.assertEqual(th.redact_credential_values(kept), kept, kept)

    def test_the_real_boundary_redacts_what_it_actually_posts(self):
        """Proves the CALL, not the function.

        A boundary that imported the redactor and never applied it would pass every test above while
        publishing the secret, so this drives `_open_upgrade_pr` itself with the git steps stubbed and
        captures the payload that would really be POSTed.
        """
        import json as _json
        import module_manager
        posted = {}

        def capture(path, tok, user_agent=None, method=None, data=None):
            posted["payload"] = _json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
            raise RuntimeError("stop here: the payload is what this test is about")

        # `_open_upgrade_pr` imports these INSIDE the function, so they resolve from sys.modules at call
        # time -- patching module attributes would miss them entirely.
        fake_client = mock.Mock()
        fake_client.request.side_effect = capture
        fake_subprocess = mock.Mock()
        fake_subprocess.CalledProcessError = subprocess.CalledProcessError
        fake_boot = mock.Mock()
        fake_boot.repo_slug.return_value = "o/r"
        fake_boot.gh_token.return_value = "live-tok-value"
        fake_identity = mock.Mock()
        fake_identity.resolve_default_branch.return_value = "main"
        with mock.patch.dict(sys.modules, {"github_client": fake_client,
                                           "subprocess": fake_subprocess,
                                           "boot": fake_boot,
                                           "repo_identity": fake_identity}):
            try:
                module_manager._open_upgrade_pr(
                    "b", "Update using live-tok-value",
                    "Scope: pushed with ghp_16C7e42F292c69 and live-tok-value.")
            except Exception:   # noqa: BLE001 - the capture above stops the real POST on purpose
                pass

        self.assertIn("payload", posted, "the boundary never reached its POST; the test proved nothing")
        rendered = _json.dumps(posted["payload"])
        self.assertNotIn("live-tok-value", rendered)
        self.assertNotIn("ghp_16C7e42F292c69", rendered)


class TestTheOtherOpenerRedactsToo(unittest.TestCase):
    """`tune.py` is the second (and, per a reviewer's search, last) function in the tree that POSTs to
    /pulls. It composes a body interpolating an operator-supplied setting value, and it got the redaction
    call in this round with nothing proving the call -- the same gap the upgrade opener's own test exists
    to close. `_open_tune_pr` is injected-only and never runs here, so without this a future edit that
    drops those two lines is invisible to the suite."""

    def test_the_tune_opener_redacts_what_it_would_post(self):
        import json as _json
        import urllib.request
        import tune
        posted = {}

        def capture(request, *a, **kw):
            posted["payload"] = _json.loads(request.data.decode("utf-8"))
            raise RuntimeError("stop here: the payload is what this test is about")

        fake_subprocess = mock.Mock()
        fake_subprocess.CalledProcessError = subprocess.CalledProcessError
        fake_boot = mock.Mock()
        fake_boot.repo_slug.return_value = "o/r"
        fake_boot.gh_token.return_value = "live-tune-token"
        fake_identity = mock.Mock()
        fake_identity.resolve_default_branch.return_value = "main"
        with mock.patch.object(urllib.request, "urlopen", side_effect=capture), \
             mock.patch.dict(sys.modules, {"subprocess": fake_subprocess, "boot": fake_boot,
                                           "repo_identity": fake_identity}):
            try:
                tune._open_tune_pr("b", "Tune using live-tune-token",
                                   "- New value: `ghp_16C7e42F292c69` and live-tune-token", ["p"])
            except Exception:   # noqa: BLE001 — the capture stops the real POST on purpose
                pass

        self.assertIn("payload", posted, "the opener never reached its POST; the test proved nothing")
        rendered = _json.dumps(posted["payload"])
        self.assertNotIn("live-tune-token", rendered)
        self.assertNotIn("ghp_16C7e42F292c69", rendered)


import contextlib   # noqa: E402
import io   # noqa: E402

import validate   # noqa: E402


class _Args:
    """A stand-in for the CLI args an adapter method receives; the currency check reads none of it."""
    rest = []


class BaseCurrencyRepo(unittest.TestCase):
    """A throwaway repo whose origin refs are set with LOCAL plumbing only (update-ref / symbolic-ref),
    never a fetch — the same discipline the check under test keeps. Default state: on `main`, current with
    origin. The `make_*` helpers move it into each judged state."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.email", "test@example.invalid")
        git(self.root, "config", "user.name", "Test")
        self._commit("seed")
        head = git(self.root, "rev-parse", "HEAD").stdout.strip()
        git(self.root, "update-ref", "refs/remotes/origin/main", head)
        git(self.root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")

    def tearDown(self):
        self._tmp.cleanup()

    def _commit(self, name):
        with open(os.path.join(self.root, name), "w", encoding="utf-8") as handle:
            handle.write(name + "\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", name)
        return git(self.root, "rev-parse", "HEAD").stdout.strip()

    def _fingerprint(self):
        return (git(self.root, "rev-parse", "HEAD").stdout.strip(),
                git(self.root, "status", "--porcelain").stdout)

    def make_wrong_base(self):
        git(self.root, "checkout", "-q", "-b", "feature")

    def make_behind(self):
        ahead = self._commit("origin-only")
        git(self.root, "update-ref", "refs/remotes/origin/main", ahead)
        git(self.root, "reset", "-q", "--hard", "HEAD~1")

    def make_diverged(self):
        self.make_behind()
        self._commit("local-only")

    def make_default_unresolvable(self):
        git(self.root, "symbolic-ref", "-d", "refs/remotes/origin/HEAD")

    def make_origin_ref_unknown(self):
        # origin/HEAD still names main, but the ref itself was never fetched here.
        git(self.root, "update-ref", "-d", "refs/remotes/origin/main")

    def make_detached_head(self):
        # No named branch: `git rev-parse --abbrev-ref HEAD` answers the literal "HEAD" here.
        head = git(self.root, "rev-parse", "HEAD").stdout.strip()
        git(self.root, "checkout", "-q", "--detach", head)


_REFUSING_STATES = (("wrong_base", "wrong-base"), ("behind", "behind-origin"), ("diverged", "diverged"))


class TestBaseCurrencyHelper(BaseCurrencyRepo):
    def test_current_carries_a_judged_against_attestation(self):
        verdict = th.judge_base_currency(root=self.root)
        self.assertEqual(verdict["status"], "current")
        self.assertFalse(verdict["refuses"])
        attest = verdict["currency"]["judged_against"]
        self.assertEqual(attest["default_branch"], "main")
        self.assertEqual(attest["origin_commit"],
                         git(self.root, "rev-parse", "refs/remotes/origin/main").stdout.strip())
        self.assertTrue(attest["fetch_age"])

    def test_a_default_branch_that_cannot_be_resolved_discloses_rather_than_refusing(self):
        self.make_default_unresolvable()
        verdict = th.judge_base_currency(root=self.root)
        self.assertEqual(verdict["status"], "unverified")
        self.assertFalse(verdict["refuses"])
        self.assertFalse(verdict["currency"]["verified"])
        self.assertIn("default branch", verdict["currency"]["note"])

    def test_an_origin_ref_never_fetched_here_discloses_rather_than_refusing(self):
        self.make_origin_ref_unknown()
        verdict = th.judge_base_currency(root=self.root)
        self.assertEqual(verdict["status"], "unverified")
        self.assertFalse(verdict["refuses"])
        self.assertIn("origin/main", verdict["currency"]["note"])

    def test_a_detached_head_reads_as_detached_not_a_branch_named_head(self):
        # DH-2 / TI-2: `git rev-parse --abbrev-ref HEAD` returns the literal "HEAD" on a detached HEAD, so the
        # wrong-base explanation must render the friendlier "a detached or unnamed HEAD", never "'HEAD'".
        self.make_detached_head()
        verdict = th.judge_base_currency(root=self.root)
        self.assertEqual(verdict["code"], "wrong-base")
        self.assertTrue(verdict["refuses"])
        self.assertIn("a detached or unnamed HEAD", verdict["explanation"])
        self.assertNotIn("'HEAD'", verdict["explanation"])

    def test_each_refusal_names_a_remedy_and_never_touches_the_remote(self):
        for maker, code in _REFUSING_STATES:
            with self.subTest(state=maker):
                self._tmp.cleanup(); self.setUp()
                getattr(self, "make_" + maker)()
                verdict = th.judge_base_currency(root=self.root)
                self.assertEqual(verdict["code"], code)
                self.assertTrue(verdict["refuses"])
                self.assertTrue(verdict["next_actions"])
                joined = " ".join(verdict["next_actions"]).lower()
                for forbidden in ("remove the remote", "re-point", "repoint", "remove origin", "set-url"):
                    self.assertNotIn(forbidden, joined)

    def test_the_check_makes_no_network_call(self):
        """Every git invocation the check issues is local plumbing. A fetch or ls-remote slipping in later
        fails here rather than reaching a remote."""
        seen = []
        original = th._git

        def spy(args, root, check=False):
            seen.append(list(args))
            return original(args, root, check=check)

        with mock.patch.object(th, "_git", spy):
            th.judge_base_currency(root=self.root)
        self.assertTrue(seen)
        for call in seen:
            self.assertIn(call[0], th._CURRENCY_GIT_SUBCOMMANDS,
                          "unexpected git subcommand in the currency check: {0}".format(call))


class TestCurrencyRefusesAtEveryAdapterEntry(BaseCurrencyRepo):
    """Obligation 1: EACH wired entry point refuses — not only the helper. The refusal fires before any
    domain call, so a refusing state leaves the tree untouched (nothing was mutated to be undone)."""

    def _adapter_entries(self):
        import transaction as tx
        import transaction_adapters_remove  # noqa: F401 — registers engine-remove
        import transaction_adapters_upgrade  # noqa: F401 — registers upgrade + rollback
        return (
            ("upgrade-adapter", lambda: tx._REGISTRY["engine-upgrade"].apply(
                _Args(), {"inputs": {"release": "v1"}})),
            ("rollback-adapter", lambda: tx._REGISTRY["engine-upgrade-rollback"].apply(_Args(), {})),
            ("remove-adapter", lambda: tx._REGISTRY["engine-remove"].apply(
                _Args(), {"inputs": {"protection": "keep"}})),
        )

    def test_each_adapter_entry_refuses_each_stale_state_without_mutating(self):
        for entry_label, run_entry in self._adapter_entries():
            for maker, code in _REFUSING_STATES:
                with self.subTest(entry=entry_label, state=maker):
                    self._tmp.cleanup(); self.setUp()
                    getattr(self, "make_" + maker)()
                    before = self._fingerprint()
                    with mock.patch.object(validate, "ROOT", self.root):
                        with self.assertRaises(transaction.TransactionRefused) as caught:
                            run_entry()
                    self.assertEqual(caught.exception.code, code)
                    self.assertEqual(self._fingerprint(), before)


class TestCurrencyRefusesAtEveryDoor(BaseCurrencyRepo):
    """The two operator-typed doors in module_manager refuse each stale state before their mutation, using
    the same shared judgment — so a door and its adapter cannot drift into two notions of a stale base."""

    def _run_upgrade_door(self):
        import module_manager
        # The consent gate runs first on this path and needs a real engine to derive a handle; short it to
        # 'matches' so the base-currency gate under test is reached. The mutation itself is stubbed so a
        # bug that let it through would be caught, not silently applied.
        with mock.patch.object(module_manager, "_refuse_stale_consent", return_value=None), \
             mock.patch.object(module_manager, "upgrade") as domain, \
             mock.patch.object(validate, "ROOT", self.root):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = module_manager.main(["upgrade", "--confirm", "--consent-handle=x"])
        return code, domain, buffer.getvalue()

    def _run_remove_door(self):
        import module_manager
        with mock.patch.object(module_manager, "remove_engine") as domain, \
             mock.patch.object(validate, "ROOT", self.root):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = module_manager.main(["remove-engine", "--confirm", "--keep-protection"])
        return code, domain, buffer.getvalue()

    def test_each_door_refuses_each_stale_state_without_applying(self):
        for door_label, run_door in (("upgrade-door", self._run_upgrade_door),
                                     ("remove-door", self._run_remove_door)):
            for maker, code in _REFUSING_STATES:
                with self.subTest(door=door_label, state=maker):
                    self._tmp.cleanup(); self.setUp()
                    getattr(self, "make_" + maker)()
                    rc, domain, out = run_door()
                    self.assertEqual(rc, 2)
                    domain.assert_not_called()
                    self.assertNotIn("re-point", out.lower())
                    self.assertNotIn("remove the remote", out.lower())

    def test_the_remove_door_under_json_emits_a_json_refusal_not_loose_prose(self):
        # SC-3: under --json a base-currency refusal must be a JSON object, never prose printed loose into the
        # JSON stream (which would corrupt a machine reader parsing stdout).
        import json as _json
        import module_manager
        self.make_wrong_base()
        with mock.patch.object(module_manager, "remove_engine") as domain, \
             mock.patch.object(validate, "ROOT", self.root):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = module_manager.main(
                    ["remove-engine", "--confirm", "--keep-protection", "--json"])
        self.assertEqual(code, 2)
        domain.assert_not_called()
        payload = _json.loads(buffer.getvalue())   # the whole stream must parse as ONE JSON document
        self.assertTrue(payload["refused"])


class TestTheModuleFlowTakesNoNewRefusal(BaseCurrencyRepo):
    """The in-tree module add/remove flow keeps its recorded current-branch design: its only pre-mutation
    gate is `refuse_unless_ready`, which never consults base currency. On a wrong AND behind base — where
    every PR-shaped entry refuses — the module gate still passes."""

    def test_refuse_unless_ready_does_not_consult_base_currency(self):
        self.make_wrong_base()
        self.make_behind()
        os.makedirs(os.path.join(self.root, ".engine", "modules"), exist_ok=True)
        state = th.refuse_unless_ready([".engine/modules/design-review.json"], root=self.root)
        self.assertNotEqual(state["branch"], "main")   # genuinely a wrong base for a PR-shaped transaction


class TestCurrencyReachesTheOperatorAndTheEnvelope(BaseCurrencyRepo):
    """Obligation 2 and 3: the note rides the machine envelope AND surfaces in the operator-facing handoff
    text, for both the unverified disclosure and the judged attestation."""

    def test_do_run_threads_the_currency_note_onto_the_envelope(self):
        note = {"verified": False, "note": "Base currency was not checked: origin unknown here."}

        class _FakeAdapter(transaction.Adapter):
            operation = "engine-upgrade"

            def inspect(self, args):
                return {"summary": "x", "fingerprints": {"a": "1"}}

            def plan(self, args, facts):
                return {"inputs": {}, "consequences": ["c"], "effects": [],
                        "reversibility": "reverted-pull-request"}

            def apply(self, args, plan):
                return {"pr": {"url": "https://example.invalid/1"}, "base_currency": note}

            def verify(self, args, applied):
                return []

            def handoff(self, args, applied, receipts):
                return {"kind": "pull-request", "summary": "done"}

        adapter = _FakeAdapter()
        args = _Args()
        _facts, planned = transaction._planned(adapter, args)   # the handle the operator would carry back
        envelope = transaction.do_run(adapter, args, planned["consent_handle"])
        self.assertEqual(envelope["currency"], note)

    def test_the_upgrade_handoff_summary_carries_the_note_for_the_operator(self):
        import transaction_adapters_upgrade as up
        note = {"verified": False, "note": "Base currency was not checked: origin/main unknown here."}
        handoff = up.UpgradeEngine().handoff(
            _Args(), {"pr": {"url": "https://example.invalid/1"}, "base_currency": note}, [])
        self.assertIn("Base currency was not checked", handoff["summary"])

    def test_the_remove_handoff_summary_carries_the_attestation_for_the_operator(self):
        import transaction_adapters_remove as rm
        note = {"verified": True, "note": "Base is current with origin/main (last fetched 1 hour ago); "
                                          "judged against abcdef123456.",
                "judged_against": {"default_branch": "main", "origin_commit": "abcdef1234567",
                                   "fetch_age": "last fetched 1 hour ago"}}
        handoff = rm.RemoveEngine().handoff(
            _Args(), {"pr": {"url": "https://example.invalid/9"}, "base_currency": note}, [])
        self.assertIn("Base is current with origin/main", handoff["summary"])


# Kept LAST on purpose: this block used to sit mid-file, so every test class below it was
# invisible to anyone running the file directly -- 19 of this build's own tests among them. CI
# uses discovery and ran them, which is the same "green over a gap" shape as the defect repaired
# here.
if __name__ == "__main__":
    unittest.main()
