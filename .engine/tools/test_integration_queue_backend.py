"""Tests for the integration backend seam — the serialized fallback's advisory label-CAS and the native stub."""

import re
import unittest
import urllib.parse

import integration_queue_backend as be

LABEL = be.INTEGRATING_LABEL


class _FakeGH:
    """A minimal GitHub label transport over an in-memory {pr_number: set(label_names)} state."""

    def __init__(self, prs):
        self.prs = {n: set(labs) for n, labs in prs.items()}

    def transport(self, method, path, body):
        if method == "GET" and "/pulls?" in path:
            return 200, [{"number": n, "labels": [{"name": x} for x in sorted(labs)]}
                         for n, labs in self.prs.items()]
        if method == "GET" and "/labels/" in path:          # label_exists probe
            return 200, {}
        m = re.search(r"/issues/(\d+)/labels$", path)
        if method == "POST" and m:
            self.prs.setdefault(int(m.group(1)), set()).update(body["labels"])
            return 200, []
        m2 = re.search(r"/issues/(\d+)/labels/(.+)$", path)
        if method == "DELETE" and m2:
            self.prs.setdefault(int(m2.group(1)), set()).discard(urllib.parse.unquote(m2.group(2)))
            return 204, None
        return 404, None


def _serialized(gh):
    return be.SerializedFallbackBackend("you/proj", "tok", transport=gh.transport)


class TestNativeStub(unittest.TestCase):
    def test_native_unavailable_discloses_plainly_and_names_the_tracking_issue(self):
        ok, why = be.NativeMergeQueueBackend().available("main")
        self.assertFalse(ok)
        self.assertIn("989", why)
        self.assertIn("merge queue", why.lower())
        self.assertNotIn("merge_group", why)      # operator-facing: no CI internals

    def test_select_backend_falls_back_to_serialized_and_discloses(self):
        gh = _FakeGH({})
        backend, why = be.select_backend("you/proj", "tok", "main", tier="solo", transport=gh.transport)
        self.assertEqual(backend.name, "serialized")
        self.assertIn("989", why)


class TestSerializedAdmission(unittest.TestCase):
    def test_admit_acquires_when_free_and_release_clears(self):
        gh = _FakeGH({7: set()})
        b = _serialized(gh)
        adm = b.admit(7)
        self.assertTrue(adm.acquired)
        self.assertEqual(b.admitted(), 7)
        b.release(7)
        self.assertIsNone(b.admitted())

    def test_admit_refuses_when_another_pr_holds(self):
        gh = _FakeGH({7: set(), 9: {LABEL}})      # PR 9 already integrating
        b = _serialized(gh)
        adm = b.admit(7)
        self.assertFalse(adm.acquired)
        self.assertEqual(adm.holder, 9)
        self.assertNotIn(LABEL, gh.prs[7])         # never stole admission

    def test_same_pr_double_admit_is_idempotent(self):
        gh = _FakeGH({7: set()})
        b = _serialized(gh)
        self.assertTrue(b.admit(7).acquired)
        self.assertTrue(b.admit(7).acquired)       # still just one holder, ours
        self.assertEqual(b.admitted(), 7)

    def test_concurrent_admission_backs_off_without_corruption(self):
        # Model a concurrent session that admits a DIFFERENT PR between our add and our re-read: our CAS sees
        # two holders and backs off, dropping our label. No exclusion guarantee is claimed — the property under
        # test is NON-corruption: we never leave two holders wedged from our own hand, and never merge.
        class _RacingGH(_FakeGH):
            def transport(self, method, path, body):
                result = super().transport(method, path, body)
                if method == "POST" and path.endswith("/labels"):
                    self.prs.setdefault(99, set()).add(LABEL)   # a peer admitted PR 99 at the same moment
                return result

        gh = _RacingGH({7: set()})
        b = _serialized(gh)
        adm = b.admit(7)
        self.assertFalse(adm.acquired)             # backed off
        self.assertNotIn(LABEL, gh.prs[7])         # dropped ours — did not wedge a second holder from our hand
        self.assertIn("backing off", adm.disclosure)


if __name__ == "__main__":
    unittest.main()
