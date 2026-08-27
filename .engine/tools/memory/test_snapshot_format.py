"""Focused acceptance evidence for the v2 plaintext multipart snapshot codec."""
from __future__ import annotations

import hashlib
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory import snapshot_format as sf  # noqa: E402


class RoundTrips(unittest.TestCase):
    def test_deterministic_observed_size_round_trip(self):
        row = b'{"kind":"turn-delta","text":"observed corpus"}\n'
        # The original failure was observed around 43 MiB.  This intentionally
        # crosses that corpus size without making the test depend on randomness.
        ledger = row * ((43 * 1024 * 1024) // len(row) + 1)
        first = sf.encode(ledger, request_limit=32768)
        second = sf.encode(ledger, request_limit=32768)
        self.assertEqual(first, second)
        self.assertEqual(sf.decode(first["manifest"], first["parts"], request_limit=32768), ledger)

    def test_boundary_round_trip_and_actual_serialized_request_cap(self):
        # Incompressible bytes force genuine multipart behaviour under the injected cap.
        ledger = bytes(range(256)) * 100
        cap = 512
        encoded = sf.encode(ledger, request_limit=cap)
        self.assertEqual(encoded["manifest"]["snapshot-format"], 2)
        self.assertGreater(len(encoded["parts"]), 1)
        self.assertTrue(all(sf.encoded_request_size(part) <= cap for part in encoded["parts"]))
        self.assertEqual(sf.decode(encoded["manifest"], encoded["parts"], request_limit=cap), ledger)


class Refusals(unittest.TestCase):
    def test_default_eight_mib_cap_measures_base64_and_json(self):
        raw = sf.max_part_bytes()
        self.assertLessEqual(sf.encoded_request_size(b"x" * raw), sf.PART_REQUEST_BYTES)
        self.assertGreater(sf.encoded_request_size(b"x" * (raw + 1)), sf.PART_REQUEST_BYTES)

    def test_one_byte_over_request_limit_is_refused(self):
        cap = 256
        raw = sf.max_part_bytes(cap)
        self.assertLessEqual(sf.encoded_request_size(b"x" * raw), cap)
        self.assertGreater(sf.encoded_request_size(b"x" * (raw + 1)), cap)
        encoded = sf.encode(b"abc", request_limit=cap)
        encoded["manifest"]["parts"][0]["bytes"] = raw + 1
        with self.assertRaises(sf.SnapshotLimitError):
            sf.decode(encoded["manifest"], encoded["parts"], request_limit=cap)

    def test_33rd_part_and_manifest_tampering_are_refused(self):
        encoded = sf.encode(bytes(range(256)) * 20, request_limit=128)
        manifest = encoded["manifest"]
        manifest["parts"] = manifest["parts"] * 33
        with self.assertRaises(sf.SnapshotLimitError):
            sf.decode(manifest, encoded["parts"] * 33, request_limit=128)

    def test_part_digest_and_compression_identity_are_strict(self):
        encoded = sf.encode(b"abc")
        encoded["manifest"]["compression"] = "zstd"
        with self.assertRaises(sf.SnapshotValidationError):
            sf.decode(encoded["manifest"], encoded["parts"])

    def test_uncompressed_and_compressed_ceilings_refuse(self):
        old_plain, old_compressed = sf.MAX_UNCOMPRESSED_BYTES, sf.MAX_COMPRESSED_BYTES
        try:
            sf.MAX_UNCOMPRESSED_BYTES = 3
            with self.assertRaises(sf.SnapshotLimitError):
                sf.encode(b"four")
            sf.MAX_UNCOMPRESSED_BYTES = old_plain
            sf.MAX_COMPRESSED_BYTES = 10
            with self.assertRaises(sf.SnapshotLimitError):
                sf.encode(b"a" * 100)
        finally:
            sf.MAX_UNCOMPRESSED_BYTES, sf.MAX_COMPRESSED_BYTES = old_plain, old_compressed


class BoundedRead(unittest.TestCase):
    def test_reader_does_not_join_parts_and_reads_in_bounded_chunks(self):
        payload = bytes(range(256)) * 400
        encoded = sf.encode(payload, request_limit=512)
        reader = sf._PartReader(tuple(encoded["parts"]))
        # The implementation's reader is the no-join seam; gzip consumes it incrementally.
        import gzip
        with gzip.GzipFile(fileobj=reader, mode="rb") as source:
            while source.read(sf._COPY_CHUNK_BYTES):
                pass
        self.assertLessEqual(reader.max_chunk_returned, sf._COPY_CHUNK_BYTES)

    def test_part_iteration_stops_after_32_parts_plus_a_sentinel(self):
        encoded = sf.encode(b"x")
        seen = []

        def endless():
            while True:
                seen.append(1)
                yield encoded["parts"][0]

        with self.assertRaises(sf.SnapshotValidationError):
            sf.decode(encoded["manifest"], endless())
        self.assertEqual(len(seen), 2, "one declared part and one surplus sentinel only")


if __name__ == "__main__":
    unittest.main()
