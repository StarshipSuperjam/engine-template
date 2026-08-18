#!/usr/bin/env python3
"""Tests for release_source — the release fetch + ref/tag resolution boundary
(StarshipSuperjam/engine-template#925 Part 5, extracted from module_manager).

The network boundaries never run in the construction repo, so every test here injects the tag-published
probe / release fetch and exercises the REAL resolution logic offline. A couple of tests drive the whole
`add` path (which resolves through release_source), so this file imports `module_manager` too."""
from __future__ import annotations
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import release_source  # noqa: E402
import module_manager  # noqa: E402  (the `add` path that resolves/fetches THROUGH release_source)


class TestReleaseSourceIsTheSingleHome(unittest.TestCase):
    """The release primitives have ONE home: release_source. A future hard-coded copy on module_manager
    (the drift this extraction removes) fails here — module_manager reaches them THROUGH the module, never
    by redefining its own."""

    def test_release_source_owns_the_primitives(self):
        for name in ("_release_api_request", "_fetch_release_tree", "_archive_tree", "_resolve_release_ref",
                     "_is_bare_version", "_release_ref_candidates", "_release_tag_published",
                     "_resolve_bare_version_tag", "_home_repository", "_release_is_missing",
                     "_NoPublishedRelease", "_BARE_VERSION"):
            self.assertTrue(hasattr(release_source, name), f"release_source is missing {name}")

    def test_module_manager_does_not_redefine_a_release_primitive(self):
        # module_manager imports release_source; it must NOT carry its own copy of a moved primitive.
        for name in ("_release_api_request", "_fetch_release_tree", "_archive_tree", "_resolve_release_ref",
                     "_release_tag_published", "_resolve_bare_version_tag", "_home_repository",
                     "_release_is_missing", "_NoPublishedRelease"):
            self.assertFalse(hasattr(module_manager, name),
                             f"module_manager redefines {name} — a hard-coded copy the single home forbids")
        self.assertIs(module_manager.release_source, release_source)


class TestReleaseApiRequest(unittest.TestCase):
    """#867: the three release/tag network boundaries (`_fetch_release_tree`, `_resolve_release_ref`,
    `_release_tag_published`) now build their GitHub Request through ONE shared helper, so the header block
    and the token resolution live in one place. These offline tests are the FIRST coverage of that block —
    the three call sites are a named inductive gap the suite never runs against the network. The load-bearing
    property is the CONDITIONAL auth: a tokenless call must send NO `Authorization` (an empty `Bearer ` would
    401 even an anonymous public-release fetch), which the pre-#867 copies preserved by hand and this helper
    must keep — hence the deliberate `if tok:` rather than github_client.request's unconditional Bearer."""

    @staticmethod
    def _headers(req):
        # urllib capitalizes header keys on store; normalize for a case-insensitive assertion.
        return {k.lower(): v for k, v in req.header_items()}

    def test_an_explicit_token_sets_a_bearer_authorization_and_the_full_header_block(self):
        req = release_source._release_api_request("/repos/acme/home/releases/latest", token="ghp_secret")
        self.assertEqual(req.full_url, "https://api.github.com/repos/acme/home/releases/latest")
        h = self._headers(req)
        self.assertEqual(h["authorization"], "Bearer ghp_secret")
        self.assertEqual(h["accept"], "application/vnd.github+json")
        self.assertEqual(h["x-github-api-version"], "2022-11-28")
        self.assertEqual(h["user-agent"], "engine-module-manager")

    def test_no_token_and_no_ambient_token_sends_no_authorization(self):
        # The anonymous public-release fetch: boot.gh_token() -> None, so NO Authorization header at all.
        import boot
        saved = boot.gh_token
        boot.gh_token = lambda: None
        try:
            req = release_source._release_api_request("/repos/acme/home/tarball/v1.0.0", token=None)
        finally:
            boot.gh_token = saved
        h = self._headers(req)
        self.assertNotIn("authorization", h)                          # the property this de-dup must preserve
        self.assertEqual(h["accept"], "application/vnd.github+json")   # the rest of the block still present
        self.assertEqual(h["x-github-api-version"], "2022-11-28")
        self.assertEqual(h["user-agent"], "engine-module-manager")

    def test_no_explicit_token_falls_back_to_the_ambient_gh_token(self):
        import boot
        saved = boot.gh_token
        boot.gh_token = lambda: "ambient_tok"
        try:
            req = release_source._release_api_request("/repos/acme/home/releases/tags/v1.0.0", token=None)
        finally:
            boot.gh_token = saved
        self.assertEqual(self._headers(req)["authorization"], "Bearer ambient_tok")

    def test_an_empty_token_string_sends_no_authorization_and_never_consults_boot(self):
        # `token=""` is "not None", so the fallback is skipped, and the empty string is falsy, so no auth
        # header — matching the pre-#867 `if tok:` truthiness exactly, never drawing an empty `Bearer `.
        import boot
        saved = boot.gh_token
        boot.gh_token = lambda: (_ for _ in ()).throw(AssertionError("consulted boot for an explicit token"))
        try:
            req = release_source._release_api_request("/repos/acme/home/tarball/main", token="")
        finally:
            boot.gh_token = saved
        self.assertNotIn("authorization", self._headers(req))

    def test_the_user_agent_is_overridable_and_defaults_to_the_module_manager_agent(self):
        default = release_source._release_api_request("/x", token="t")
        custom = release_source._release_api_request("/x", token="t", user_agent="engine-something-else")
        self.assertEqual(self._headers(default)["user-agent"], "engine-module-manager")
        self.assertEqual(self._headers(custom)["user-agent"], "engine-something-else")

    def test_a_path_without_a_leading_slash_is_refused(self):
        # The path is joined onto the host verbatim, so a slash-less path would silently build a malformed
        # URL (https://api.github.comrepos/...); the helper must refuse it loudly, not emit a bad request.
        with self.assertRaises(ValueError):
            release_source._release_api_request("repos/acme/home/releases/latest", token="t")


class TestBareVersionTagResolution(unittest.TestCase):
    """#760: the manifest records the engine release BARE (`_bump_engine_manifest` strips a leading `v`), so
    `add`/`upgrade` must resolve that bare version to the home's REAL published tag before fetching — a
    `v`-tagging home was fetched as `tarball/0.4.1` and 404'd. `_release_tag_published` is the single network
    boundary; every test below injects it so the REAL resolution logic runs offline."""

    def test_is_bare_version_matches_only_a_plain_semver(self):
        self.assertTrue(release_source._is_bare_version("0.4.1"))
        self.assertTrue(release_source._is_bare_version("12.0.30"))
        self.assertFalse(release_source._is_bare_version("v0.4.1"))    # a real tag, not bare
        self.assertFalse(release_source._is_bare_version("main"))      # a branch
        self.assertFalse(release_source._is_bare_version("abc1234"))   # a sha
        self.assertFalse(release_source._is_bare_version("latest"))
        self.assertFalse(release_source._is_bare_version(None))

    def test_release_ref_candidates_probe_v_first(self):
        # v-first so the dominant convention (and the `v` that _bump_engine_manifest strips) resolves in one hit.
        self.assertEqual(release_source._release_ref_candidates("0.4.1"), ["v0.4.1", "0.4.1"])

    def test_bare_version_resolves_to_the_v_tag_on_a_v_home(self):
        saved = release_source._release_tag_published
        release_source._release_tag_published = lambda tag, repo=None, token=None: tag in {"v0.4.1", "v0.4.0"}
        try:
            self.assertEqual(release_source._resolve_release_ref("0.4.1", repo="acme/home"), "v0.4.1")
        finally:
            release_source._release_tag_published = saved

    def test_bare_version_falls_back_to_the_bare_tag_on_a_bare_home(self):
        saved = release_source._release_tag_published
        release_source._release_tag_published = lambda tag, repo=None, token=None: tag == "0.4.1"
        try:
            self.assertEqual(release_source._resolve_release_ref("0.4.1", repo="acme/home"), "0.4.1")
        finally:
            release_source._release_tag_published = saved

    def test_a_pinned_tag_or_sha_passes_through_without_a_probe(self):
        # A non-bare ref must never touch the network — the tag-pin supply-chain control is unchanged.
        saved = release_source._release_tag_published
        release_source._release_tag_published = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("probed a pinned ref"))
        try:
            self.assertEqual(release_source._resolve_release_ref("v0.4.1", repo="acme/home"), "v0.4.1")
            self.assertEqual(release_source._resolve_release_ref("abc1234def", repo="acme/home"), "abc1234def")
        finally:
            release_source._release_tag_published = saved

    def test_no_matching_release_is_classified_missing_not_transport(self):
        saved = release_source._release_tag_published
        release_source._release_tag_published = lambda *a, **k: False   # the home publishes no such release
        try:
            with self.assertRaises(release_source._NoPublishedRelease) as ctx:
                release_source._resolve_release_ref("0.4.1", repo="acme/home")
        finally:
            release_source._release_tag_published = saved
        self.assertTrue(release_source._release_is_missing(ctx.exception))   # refuse loudly, never degrade

    def test_a_transport_fault_on_the_probe_propagates_and_degrades(self):
        import urllib.error
        saved = release_source._release_tag_published
        release_source._release_tag_published = lambda *a, **k: (_ for _ in ()).throw(
            urllib.error.URLError("network down"))
        try:
            with self.assertRaises(urllib.error.URLError) as ctx:
                release_source._resolve_release_ref("0.4.1", repo="acme/home")
        finally:
            release_source._release_tag_published = saved
        self.assertFalse(release_source._release_is_missing(ctx.exception))  # transport -> degrade, not refuse

    def test_add_resolves_the_bare_recorded_version_to_the_tag_before_fetching(self):
        # End to end on the real add path: the bare recorded "0.0.0" is resolved to "v0.0.0" and THAT is what
        # the fetch is asked for — the exact wiring that fixes #760.
        seen = {}
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            with module_manager._redirect_root(live):
                module_manager._build_add_fixture(live)                  # engine_release "0.0.0", v-less
                saved_pub = release_source._release_tag_published
                saved_fetch = release_source._fetch_release_tree
                release_source._release_tag_published = lambda tag, repo=None, token=None: tag == "v0.0.0"

                def _spy(ref, dest, repo=None, token=None):
                    seen["ref"] = ref
                    raise RuntimeError("stop after capturing the resolved ref")
                release_source._fetch_release_tree = _spy
                try:
                    module_manager.add("feat")
                finally:
                    release_source._fetch_release_tree = saved_fetch
                    release_source._release_tag_published = saved_pub
        self.assertEqual(seen.get("ref"), "v0.0.0")   # fetched the resolved tag, not the bare "0.0.0"

    def test_the_760_falsification_demo_passes(self):
        # Runs the shipped #760 demo (its negative control reproduces the original 404). This surviving
        # reference is also what lets the demo travel (census-completeness) rather than retire.
        import demo_760_add_release_tag as demo
        import quiet_call
        self.assertEqual(quiet_call.run(demo.main), 0)
