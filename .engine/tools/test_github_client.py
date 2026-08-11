"""Unit tests for github_client — the shared authenticated GitHub REST API client.

Run via the engine suite: `uv run --directory .engine --frozen -- python tools/selftest.py`.
Fully offline: the only network call goes through `github_client._urlopen`, which every test here replaces
with an in-memory fake, so the REAL request-building, off-host guard, pagination, and decode logic runs with
no token and no network.
"""

import base64
import io
import json
import os
import sys
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # .engine/tools on path
import github_client  # noqa: E402


def _headers(req) -> dict:
    """Case-insensitive header view of a urllib Request (urllib title-cases header keys)."""
    return {k.lower(): v for k, v in req.header_items()}


class _FakeResp:
    """A minimal urlopen() context-manager stand-in: body bytes + an optional Link header."""

    def __init__(self, body, link=None, status=200):
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.status = status
        self.headers = {"Link": link} if link is not None else {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeNetwork:
    """Records each request and serves a queued response (or raises a queued error)."""

    def __init__(self):
        self.requests = []
        self._responses = []

    def queue(self, resp):
        self._responses.append(resp)
        return self

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


class RequestBuilderTests(unittest.TestCase):
    def test_relative_path_is_prefixed_with_the_api_host(self):
        req = github_client.request("/repos/o/r/pulls/1", "tok", user_agent="ua")
        self.assertEqual(req.full_url, "https://api.github.com/repos/o/r/pulls/1")

    def test_headers_carry_bearer_auth_accept_version_and_user_agent(self):
        h = _headers(github_client.request("/x", "secret-tok", user_agent="engine-test-ua"))
        self.assertEqual(h["authorization"], "Bearer secret-tok")
        self.assertEqual(h["accept"], "application/vnd.github+json")
        self.assertEqual(h["x-github-api-version"], "2022-11-28")
        self.assertEqual(h["user-agent"], "engine-test-ua")

    def test_get_request_sends_no_content_type(self):
        # A GET caller's headers stay byte-identical to the old read-only builders (no Content-Type).
        req = github_client.request("/x", "tok", user_agent="ua")
        self.assertNotIn("content-type", _headers(req))
        self.assertIsNone(req.data)
        self.assertEqual(req.get_method(), "GET")

    def test_content_type_appears_only_when_a_body_is_sent(self):
        body = json.dumps({"name": "x"}).encode("utf-8")
        req = github_client.request("/labels", "tok", user_agent="ua", method="POST", data=body)
        self.assertEqual(_headers(req)["content-type"], "application/json")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.data, body)


class OffHostGuardTests(unittest.TestCase):
    """The security falsification: a token-bearing request must never be redirected off-host by a
    crafted Link header. An absolute URL off the GitHub API host MUST raise; this is the one behavior the
    extraction may never weaken."""

    def test_an_off_host_absolute_url_raises(self):
        with self.assertRaises(ValueError):
            github_client.request("https://evil.example.com/repos/o/r", "tok", user_agent="ua")

    def test_a_lookalike_subdomain_host_raises(self):
        with self.assertRaises(ValueError):
            github_client.request("https://api.github.com.evil.example/x", "tok", user_agent="ua")

    def test_an_on_host_absolute_url_is_allowed_verbatim(self):
        url = "https://api.github.com/repositories/1/pulls/1/files?per_page=100&page=2"
        req = github_client.request(url, "tok", user_agent="ua")
        self.assertEqual(req.full_url, url)


class GetTests(unittest.TestCase):
    def setUp(self):
        self._orig = github_client._urlopen

    def tearDown(self):
        github_client._urlopen = self._orig

    def test_get_json_returns_the_parsed_body(self):
        github_client._urlopen = _FakeNetwork().queue(_FakeResp({"changed_files": 7}))
        self.assertEqual(github_client.get_json("/repos/o/r/pulls/1", "tok", user_agent="ua"),
                         {"changed_files": 7})

    def test_get_json_raises_httperror_unwrapped_on_4xx(self):
        # lock_integrity's 404 branch and the guards' fail-closed posture both depend on this propagating
        # the raw urllib.error.HTTPError, not a wrapped/normalized error.
        err = urllib.error.HTTPError("https://api.github.com/x", 404, "Not Found", None, io.BytesIO(b""))
        github_client._urlopen = _FakeNetwork().queue(err)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            github_client.get_json("/x", "tok", user_agent="ua")
        self.assertEqual(ctx.exception.code, 404)

    def test_get_page_returns_body_and_link_header(self):
        net = _FakeNetwork().queue(_FakeResp([{"filename": "a"}], link='<https://api.github.com/x?page=2>; rel="next"'))
        github_client._urlopen = net
        body, link = github_client.get_page("/x?page=1", "tok", user_agent="ua")
        self.assertEqual(body, [{"filename": "a"}])
        self.assertIn('rel="next"', link)

    def test_get_page_user_agent_reaches_the_request(self):
        net = _FakeNetwork().queue(_FakeResp([]))
        github_client._urlopen = net
        github_client.get_page("/x", "tok", user_agent="distinct-ua")
        self.assertEqual(_headers(net.requests[0])["user-agent"], "distinct-ua")


class JsonRequestTests(unittest.TestCase):
    """json_request — the shared status-returning JSON transport promoted here in #907. It BUILDS via
    request() (so the off-host guard/headers stay single-homed), executes through _urlopen, returns
    (status, data|None), maps HTTPError->(code, None), and lets URLError propagate."""

    def setUp(self):
        self._orig = github_client._urlopen

    def tearDown(self):
        github_client._urlopen = self._orig

    def test_get_returns_status_and_parsed_body(self):
        github_client._urlopen = _FakeNetwork().queue(_FakeResp({"number": 7}, status=200))
        self.assertEqual(github_client.json_request("GET", "/repos/o/r/pulls/7", "tok", user_agent="ua"),
                         (200, {"number": 7}))

    def test_post_encodes_body_sets_method_and_content_type(self):
        net = _FakeNetwork().queue(_FakeResp({"id": 1}, status=201))
        github_client._urlopen = net
        status, data = github_client.json_request(
            "POST", "/repos/o/r/labels", "tok", user_agent="ua", body={"name": "engine"})
        self.assertEqual((status, data), (201, {"id": 1}))
        req = net.requests[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(_headers(req)["content-type"], "application/json")
        self.assertEqual(json.loads(req.data.decode("utf-8")), {"name": "engine"})

    def test_patch_and_delete_methods_reach_the_request(self):
        for method, body in (("PATCH", {"state": "closed"}), ("DELETE", None)):
            net = _FakeNetwork().queue(_FakeResp({}, status=200))
            github_client._urlopen = net
            github_client.json_request(method, "/x", "tok", user_agent="ua", body=body)
            self.assertEqual(net.requests[0].get_method(), method)

    def test_http_status_is_propagated_as_status_not_raised(self):
        # a 404 is DATA to the caller (it branches on the status), never an exception (label_exists depends on this)
        err = urllib.error.HTTPError("https://api.github.com/x", 404, "Not Found", None, io.BytesIO(b""))
        github_client._urlopen = _FakeNetwork().queue(err)
        self.assertEqual(github_client.json_request("GET", "/x", "tok", user_agent="ua"), (404, None))

    def test_a_5xx_is_also_returned_as_status(self):
        err = urllib.error.HTTPError("https://api.github.com/x", 503, "Unavailable", None, io.BytesIO(b""))
        github_client._urlopen = _FakeNetwork().queue(err)
        self.assertEqual(github_client.json_request("POST", "/x", "tok", user_agent="ua", body={}), (503, None))

    def test_unreachable_host_propagates_urlerror_unwrapped(self):
        # the one failure whose MEANING differs by caller (read-degrade vs write-fail) — never swallowed here
        github_client._urlopen = _FakeNetwork().queue(urllib.error.URLError("no route"))
        with self.assertRaises(urllib.error.URLError):
            github_client.json_request("GET", "/x", "tok", user_agent="ua")

    def test_empty_body_decodes_to_none_not_a_json_error(self):
        # a 204 DELETE / no-content PATCH: a SUCCESSFUL write returns (status, None), never a JSONDecodeError
        github_client._urlopen = _FakeNetwork().queue(_FakeResp(b"", status=204))
        self.assertEqual(github_client.json_request("DELETE", "/x", "tok", user_agent="ua"), (204, None))

    def test_off_host_guard_still_bites_through_json_request(self):
        # json_request builds via request(), so the off-host guard is intact for an absolute URL
        with self.assertRaises(ValueError):
            github_client.json_request("GET", "https://evil.example.com/x", "tok", user_agent="ua")

    def test_bearer_auth_and_user_agent_reach_the_request(self):
        net = _FakeNetwork().queue(_FakeResp({}, status=200))
        github_client._urlopen = net
        github_client.json_request("GET", "/x", "sekret", user_agent="distinct-ua")
        h = _headers(net.requests[0])
        self.assertEqual(h["authorization"], "Bearer sekret")
        self.assertEqual(h["user-agent"], "distinct-ua")


class ReaderTests(unittest.TestCase):
    """reader() — the neutral (.repo + .transport) seam a generic consumer takes instead of a domain
    client's private transport (issue #907)."""

    def setUp(self):
        self._orig = github_client._urlopen

    def tearDown(self):
        github_client._urlopen = self._orig

    def test_reader_binds_repo_and_a_working_transport(self):
        net = _FakeNetwork().queue(_FakeResp([{"number": 1}], status=200))
        github_client._urlopen = net
        r = github_client.reader("o/r", "tok", user_agent="ua")
        self.assertEqual(r.repo, "o/r")
        self.assertEqual(r.transport("GET", "/repos/o/r/pulls", None), (200, [{"number": 1}]))
        self.assertEqual(_headers(net.requests[0])["user-agent"], "ua")

    def test_injected_transport_overrides_the_bound_closure(self):
        # the test/demo seam: a canned transport runs the consumer logic offline (network never touched)
        seen = []

        def canned(method, path, body=None):
            seen.append((method, path))
            return 200, {"ok": True}

        github_client._urlopen = _FakeNetwork()  # would raise IndexError if the bound closure were used
        r = github_client.reader("o/r", "tok", user_agent="ua", transport=canned)
        self.assertEqual(r.transport("GET", "/x", None), (200, {"ok": True}))
        self.assertEqual(seen, [("GET", "/x")])

    def test_reader_transport_lets_urlerror_propagate(self):
        github_client._urlopen = _FakeNetwork().queue(urllib.error.URLError("down"))
        r = github_client.reader("o/r", "tok", user_agent="ua")
        with self.assertRaises(urllib.error.URLError):
            r.transport("GET", "/x", None)


class NextLinkTests(unittest.TestCase):
    def test_parses_rel_next_and_returns_none_when_absent(self):
        header = ('<https://api.github.com/r/1/files?page=2>; rel="next", '
                  '<https://api.github.com/r/1/files?page=9>; rel="last"')
        self.assertEqual(github_client.next_link(header), "https://api.github.com/r/1/files?page=2")
        self.assertIsNone(github_client.next_link('<https://api.github.com/r/1/files?page=9>; rel="last"'))
        self.assertIsNone(github_client.next_link(None))


class DecodeContentTests(unittest.TestCase):
    def _obj(self, text, encoding="utf-8"):
        return {"encoding": "base64", "content": base64.b64encode(text.encode(encoding)).decode()}

    def test_utf8_decode_round_trips(self):
        self.assertEqual(github_client.decode_content(self._obj("héllo — recall")), "héllo — recall")

    def test_utf8_sig_strips_a_committed_byte_order_mark(self):
        obj = self._obj("﻿settled doc")
        # the default utf-8 codec leaves the BOM; utf-8-sig strips it (lock_integrity's BOM tolerance)
        self.assertEqual(github_client.decode_content(obj), "﻿settled doc")
        self.assertEqual(github_client.decode_content(obj, codec="utf-8-sig"), "settled doc")

    def test_bad_bytes_are_lossy_not_fatal(self):
        obj = {"content": base64.b64encode(b"\xff\xfeok").decode()}
        # never raises — matches the callers' "replace" tolerance
        self.assertIn("ok", github_client.decode_content(obj))


if __name__ == "__main__":
    unittest.main()
