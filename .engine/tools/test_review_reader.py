"""Protocol-level tests of the reviewer-only reader, with private disposable roots."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import review_reader as reader  # noqa: E402
from mcp_test_support import call_tool_expect_error, call_tool_json, list_tool_objects


class ReaderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.library = self.base / "plans"
        self.library.mkdir()
        self.root_patch = patch.object(reader, "ROOT", self.repo)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.lib_patch = patch.object(reader.plan_store, "library_root", return_value=self.library)
        self.lib_patch.start()
        self.addCleanup(self.lib_patch.stop)

    async def read(self, path):
        return await call_tool_json(reader.server, "read_file", {"path": str(path)})

    async def denied(self, path):
        return await call_tool_expect_error(reader.server, "read_file", {"path": str(path)})

    def registered(self):
        directory = self.library / "test--abcdef"
        packets = directory / "scoped-packets"
        packets.mkdir(parents=True)
        packet = packets / "sa_test.md"
        packet.write_text("Frozen packet\n")
        supplement = packets / "clarification.txt"
        supplement.write_text("A clarification\n")
        digest = lambda p: "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()
        companion = directory / "scoped-agent-evidence.v1.json"
        companion.write_text(json.dumps({"schema_version": "scoped-agent-evidence.v1", "assignments": {
            "sa_test": {"packet_path": str(packet), "file_digest": digest(packet),
                        "supplements": [{"path": str(supplement), "digest": digest(supplement)}]}}}))
        return packet, supplement, companion

    async def test_inventory_has_only_compact_read_tool(self):
        tools = await list_tool_objects(reader.server)
        self.assertEqual([t.name for t in tools], ["read_file"])
        self.assertLess(len(tools[0].description), 200)
        self.assertTrue(tools[0].annotations.read_only_hint)
        self.assertTrue(tools[0].annotations.idempotent_hint)
        self.assertFalse(tools[0].annotations.open_world_hint)

    async def test_registered_multipart_paths_are_exact_and_integrity_checked(self):
        import scoped_agents
        import build_coordinator_core as core
        packet, _, companion = self.registered()
        packet.write_bytes(("α\r\n" * 20000).encode())
        record = json.loads(companion.read_text())
        a = record["assignments"]["sa_test"]
        a.update(id="sa_" + "a" * 32, packet_digest=core.digest(packet.read_bytes()),
                 file_digest=core.digest(packet.read_bytes()))
        a["transport"] = scoped_agents._freeze_transport(packet, packet.read_bytes(), a["id"], a["packet_digest"])
        record["read_protocol"] = scoped_agents.READ_PROTOCOL
        companion.write_text(json.dumps(record))
        parts = a["transport"]["manifest"]["pieces"]
        bodies = [(await self.read(p["path"]))["content"] for p in parts]
        self.assertEqual("".join(bodies).encode(), packet.read_bytes())
        await self.read(a["transport"]["manifest_path"])
        neighbor = packet.with_name("neighbor.txt"); neighbor.write_text("not registered")
        await self.denied(neighbor)
        Path(parts[1]["path"]).write_text("changed")
        await self.denied(parts[0]["path"])

    async def test_repository_read_is_complete_and_exact(self):
        path = self.repo / "source.py"
        path.write_text("# Unicode café\n")
        result = await self.read("source.py")
        self.assertEqual(result, {"file_path": str(path), "content": path.read_text(),
            "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
            "complete": True, "offset": 0})

    async def test_line_ranges_preserve_unicode_newlines_and_whole_digest(self):
        path = self.repo / "source.txt"
        data = "α first\r\nβ second\nγ third".encode("utf-8")
        path.write_bytes(data)
        expected_digest = "sha256:" + hashlib.sha256(data).hexdigest()
        async def selected(**kwargs):
            return await call_tool_json(reader.server, "read_file", {"path": str(path), **kwargs})
        part = await selected(offset=1, limit=1)
        self.assertEqual(part["content"], "β second\n")
        self.assertEqual(part["offset"], 1)
        self.assertFalse(part["complete"])
        self.assertEqual(part["sha256"], expected_digest)
        self.assertFalse((await selected(limit=1))["complete"])
        self.assertEqual((await selected(offset=2))["content"], "γ third")
        for kwargs in ({}, {"offset": 0, "limit": 3}, {"limit": 99}):
            result = await selected(**kwargs)
            self.assertTrue(result["complete"])
            self.assertEqual(result["content"], data.decode("utf-8"))
            self.assertEqual(result["sha256"], expected_digest)

    async def test_invalid_ranges_are_refused_and_empty_file_is_complete(self):
        path = self.repo / "source.txt"
        path.write_text("one\ntwo\n")
        for kwargs in ({"offset": -1}, {"offset": 2}, {"offset": True}, {"offset": 1.0},
                       {"offset": "1"}, {"limit": 0}, {"limit": -1}, {"limit": True},
                       {"limit": 1.0}, {"limit": "1"}):
            await call_tool_expect_error(reader.server, "read_file", {"path": str(path), **kwargs})
        path.write_text("")
        self.assertTrue((await self.read(path))["complete"])
        await call_tool_expect_error(reader.server, "read_file", {"path": str(path), "offset": 1})

    async def test_external_unregistered_and_parent_traversal_refused(self):
        external = self.base / "secret.txt"
        external.write_text("private")
        await self.denied(external)
        await self.denied("../secret.txt")
        await self.denied("")

    async def test_symlink_escape_and_directory_symlink_escape_refused(self):
        external = self.base / "secret.txt"
        external.write_text("private")
        (self.repo / "escape").symlink_to(external)
        (self.repo / "parent").symlink_to(self.base, target_is_directory=True)
        await self.denied("escape")
        await self.denied("parent/secret.txt")

    async def test_fifo_directory_binary_and_missing_refused(self):
        fifo = self.repo / "fifo"
        os.mkfifo(fifo)
        await self.denied(fifo)
        await self.denied(self.repo)
        await self.denied("missing")
        binary = self.repo / "binary"
        for data in (b"text\x00binary", b"\xff"):
            binary.write_bytes(data)
            await self.denied(binary)

    async def test_size_limit_never_returns_partial_read(self):
        path = self.repo / "large"
        path.write_bytes(b"x" * reader.MAX_BYTES)
        self.assertTrue((await self.read(path))["complete"])
        path.write_bytes(b"x" * (reader.MAX_BYTES + 1))
        self.assertIn("limit", await self.denied(path))

    async def test_registered_packet_and_supplement_only_and_digest_verified(self):
        packet, supplement, companion = self.registered()
        before = companion.read_bytes()
        self.assertEqual((await self.read(packet))["content"], packet.read_text())
        self.assertEqual((await self.read(supplement))["content"], supplement.read_text())
        unregistered = packet.parent / "unregistered.txt"
        unregistered.write_text("unregistered")
        await self.denied(unregistered)
        await self.denied(companion)
        packet.write_text("changed")
        self.assertIn("digest", await self.denied(packet))
        self.assertEqual(companion.read_bytes(), before)

    async def test_registration_cannot_grant_external_path_or_symlink_escape(self):
        packet, supplement, companion = self.registered()
        outside_file = self.base / "outside.txt"
        outside_file.write_text("unregistered outside fixture")
        record = json.loads(companion.read_text())
        record["assignments"]["sa_test"]["packet_path"] = str(outside_file)
        companion.write_text(json.dumps(record))
        await self.denied(outside_file)
        packet.unlink()
        packet.symlink_to(outside_file)
        await self.denied(packet)

    async def test_malformed_companion_is_visible_refusal(self):
        packet, supplement, companion = self.registered()
        for record in ([], {"schema_version": "scoped-agent-evidence.v1", "assignments": []},
                       {"schema_version": "scoped-agent-evidence.v1", "assignments": {"bad": None}},
                       {"schema_version": "scoped-agent-evidence.v1", "assignments": {"bad": {"supplements": [None]}}}):
            companion.write_text(json.dumps(record))
            self.assertIn("Read refused", await self.denied(packet))
        companion.write_text("{")
        self.assertIn("Read refused", await self.denied(packet))

    def test_no_follow_open_rejects_swapped_file_and_parent(self):
        outside_file = self.base / "outside.txt"
        outside_file.write_text("unregistered outside fixture")
        path = self.repo / "target"
        path.symlink_to(outside_file)
        with self.assertRaises(OSError):
            reader._bytes(path, reader.MAX_BYTES)
        folder = self.repo / "folder"
        folder.symlink_to(self.base, target_is_directory=True)
        with self.assertRaises(OSError):
            reader._bytes(folder / "outside.txt", reader.MAX_BYTES)

    async def test_stdio_launch_inventory_and_source_read(self):
        # The child reads only its own script; never asks for canonical operator plan storage.
        from mcp import ClientSession, StdioServerParameters, stdio_client
        script = Path(reader.__file__).resolve()
        params = StdioServerParameters(command=sys.executable, args=[str(script)])

        async def exercise():
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    inventory = await client.list_tools()
                    self.assertEqual([t.name for t in inventory.tools], ["read_file"])
                    response = await client.call_tool("read_file", {"path": str(script)})
                    self.assertFalse(response.is_error)
                    result = json.loads(response.content[0].text)
                    self.assertEqual(result["content"], script.read_text())
                    self.assertTrue(result["complete"])
        await asyncio.wait_for(exercise(), 15)

    def test_help_and_unknown_arguments_never_run_server(self):
        with patch.object(reader.server, "run") as run, patch("builtins.print"):
            self.assertEqual(reader.main(["--help"]), 0)
            self.assertEqual(reader.main(["--unknown"]), 2)
            self.assertEqual(reader.main(["read"]), 2)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
