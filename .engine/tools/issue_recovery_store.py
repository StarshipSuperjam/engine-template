"""Git-data backing store for durable Engine issue-recovery journals.

This module deliberately knows only enough about a journal to keep its Git
history and immutable record identities safe.  ``issue_recovery`` owns the
closed lifecycle schema and state transitions.  The adapter has no credential
or GitHub-client construction path: callers provide the already-bound client.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re


REF = "refs/heads/codex/engine-issue-recovery"
PATH = ".engine/issue-recovery/journal.json"

_SCHEMA = "issue-recovery.v1"
_MAX_RECORD_BYTES = 1024 * 1024
_MAX_SNAPSHOT_BYTES = 10 * 1024 * 1024
_MAX_HISTORY = 10_000
_SHA = re.compile(r"^[0-9a-f]{40}$")
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_UPDATE_MESSAGE = "Update Engine issue recovery journal"
_INITIAL_MESSAGE = "Initialize Engine issue recovery journal"
_GRAPH_BATCH = 20
_GRAPH_BYTES = 10 * 1024 * 1024

# Only immutable metadata travels in a history page. Blob sizes bound the
# separate payload batches; every page is pinned to the already-read tip SHA.
_HISTORY_QUERY = """
query RecoveryHistory($owner: String!, $name: String!, $expression: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    object(expression: $expression) { oid ... on Commit {
      history(first: 100, after: $after) {
        nodes { oid message parents(first: 2) { nodes { oid } pageInfo { hasNextPage } }
          tree { oid entries { name mode type oid object { oid ... on Tree {
            entries { name mode type oid object { oid ... on Tree {
              entries { name mode type oid object { ... on Blob { byteSize } } }
            } } }
          } } } }
        }
        pageInfo { hasNextPage endCursor }
      }
    } }
  }
}
"""

class RecoveryError(ValueError):
    """The durable journal cannot safely be used."""


class Conflict(RecoveryError):
    """Another writer advanced the journal; the caller must reload."""


def _is_sha(value):
    return isinstance(value, str) and bool(_SHA.fullmatch(value))


def _canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RecoveryError("journal data is not JSON serializable") from exc


def _decode_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate journal key')
            result[key] = value
        return result
    def constant(_value):
        raise ValueError('nonfinite journal value')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def _object_sha(kind, raw):
    return hashlib.sha1(kind.encode("ascii") + b" " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()


def _tree_sha(entries):
    def serialized_mode(mode):
        # Git's tree object uses ``40000`` for a directory.  GitHub's REST
        # representation spells the same mode ``040000``.
        return "40000" if mode == "040000" else mode

    raw = b"".join(
        (serialized_mode(entry["mode"]) + " " + entry["path"]).encode("utf-8") + b"\0" + bytes.fromhex(entry["sha"])
        for entry in sorted(entries, key=lambda item: item["path"].encode("utf-8"))
    )
    return _object_sha("tree", raw)


def _activation(value):
    if not isinstance(value, dict) or set(value) != {"repository_id", "genesis"}:
        raise RecoveryError("recovery activation is malformed")
    repository_id = value.get("repository_id")
    genesis = value.get("genesis")
    if not isinstance(repository_id, int) or isinstance(repository_id, bool) or repository_id <= 0 or not _is_sha(genesis):
        raise RecoveryError("recovery activation is malformed")
    return {"repository_id": repository_id, "genesis": genesis}


def _snapshot(value, repository_id):
    if not isinstance(value, dict) or set(value) != {"schema_version", "repository_id", "revision", "records"}:
        raise RecoveryError("recovery journal has an invalid shape")
    if value["schema_version"] != _SCHEMA or value["repository_id"] != repository_id:
        raise RecoveryError("recovery journal identity does not match activation")
    revision = value["revision"]
    records = value["records"]
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0 or not isinstance(records, dict):
        raise RecoveryError("recovery journal has an invalid shape")
    if len(_canonical(value)) > _MAX_SNAPSHOT_BYTES:
        raise RecoveryError("recovery journal exceeds the 10 MiB limit")
    for key, record in records.items():
        if not isinstance(key, str) or not key or not isinstance(record, dict):
            raise RecoveryError("recovery journal contains an invalid record")
        if len(_canonical(record)) > _MAX_RECORD_BYTES:
            raise RecoveryError("recovery record exceeds the 1 MiB limit")
    return value


def _initial(repository_id):
    return {"schema_version": _SCHEMA, "repository_id": repository_id, "revision": 0, "records": {}}


class GitStore:
    """A fixed-ref, append-only snapshot journal for one activated repository."""

    def __init__(self, client, activation):
        repo = getattr(client, "repo", None)
        transport = getattr(client, "_transport", None)
        if not isinstance(repo, str) or not _REPO.fullmatch(repo) or not callable(transport):
            raise RecoveryError("recovery store requires a bound GitHub client")
        self.client = client
        self.activation = _activation(activation)
        self._last_tip = None
        self._last_snapshot = None
        self._last_depth = 0

    def _call(self, method, path, body=None):
        try:
            answer = self.client._transport(method, path, body)
            if not isinstance(answer, tuple) or len(answer) != 2:
                raise TypeError
            status, data = answer
        except Exception as exc:  # transport details can include credentials or response bodies
            raise RecoveryError("Git-data service is unavailable") from exc
        if not isinstance(status, int):
            raise RecoveryError("Git-data service returned an invalid response")
        return status, data

    def _path(self, suffix):
        return f"/repos/{self.client.repo}{suffix}"

    def _repo_id(self):
        status, data = self._call("GET", self._path(""))
        if status != 200 or not isinstance(data, dict):
            raise RecoveryError("cannot verify the recovery repository")
        repository_id = data.get("id")
        if not isinstance(repository_id, int) or isinstance(repository_id, bool) or repository_id <= 0:
            raise RecoveryError("recovery repository returned an invalid identity")
        if repository_id != self.activation["repository_id"]:
            raise RecoveryError("recovery repository does not match activation")
        return repository_id

    def _ref(self):
        status, data = self._call("GET", self._path("/git/ref/heads/codex/engine-issue-recovery"))
        if status == 404:
            raise RecoveryError("the activated recovery journal ref is missing")
        if status != 200 or not isinstance(data, dict):
            raise RecoveryError("cannot read the recovery journal ref")
        obj = data.get("object")
        sha = obj.get("sha") if isinstance(obj, dict) else None
        if not _is_sha(sha) or data.get("ref") != REF or obj.get("type") != "commit":
            raise RecoveryError("recovery journal ref is malformed")
        return sha

    def _graph(self, query, variables):
        status, data = self._call("POST", "/graphql", {"query": query, "variables": variables})
        if status != 200 or not isinstance(data, dict) or set(data) != {"data"} or not isinstance(data["data"], dict):
            raise RecoveryError("recovery journal GraphQL response is unavailable or malformed")
        return data["data"]

    @staticmethod
    def _graph_entries(value):
        if not isinstance(value, list):
            raise RecoveryError("recovery journal tree is malformed")
        entries = []
        for entry in value:
            if not isinstance(entry, dict):
                raise RecoveryError("recovery journal tree is malformed")
            path, mode, kind, entry_sha = entry.get("name"), entry.get("mode"), entry.get("type"), entry.get("oid")
            # GraphQL's TreeEntry.mode is an Int (the POSIX decimal value),
            # whereas REST spells Git's octal mode as a string.
            if type(mode) is not int or mode not in (16384, 33188):
                raise RecoveryError("recovery journal tree mode is malformed")
            mode = "040000" if mode == 16384 else "100644"
            if not isinstance(path, str) or not isinstance(kind, str) or not _is_sha(entry_sha):
                raise RecoveryError("recovery journal tree is malformed")
            entries.append({"path": path, "mode": mode, "type": kind.lower(), "sha": entry_sha, "object": entry.get("object")})
        return entries

    def _graph_snapshot_pointer(self, tree):
        if not isinstance(tree, dict) or not _is_sha(tree.get("oid")):
            raise RecoveryError("recovery journal tree is malformed")
        root = self._graph_entries(tree.get("entries"))
        if _tree_sha(root) != tree["oid"] or len(root) != 1 or root[0]["path"] != ".engine" or root[0]["mode"] != "040000" or root[0]["type"] != "tree":
            raise RecoveryError("recovery journal tree contains unexpected paths")
        engine = root[0]["object"]
        if not isinstance(engine, dict) or engine.get("oid") != root[0]["sha"]:
            raise RecoveryError("recovery journal tree is malformed")
        middle = self._graph_entries(engine.get("entries"))
        if _tree_sha(middle) != root[0]["sha"] or len(middle) != 1 or middle[0]["path"] != "issue-recovery" or middle[0]["mode"] != "040000" or middle[0]["type"] != "tree":
            raise RecoveryError("recovery journal tree contains unexpected paths")
        recovery = middle[0]["object"]
        if not isinstance(recovery, dict) or recovery.get("oid") != middle[0]["sha"]:
            raise RecoveryError("recovery journal tree is malformed")
        leaf = self._graph_entries(recovery.get("entries"))
        if _tree_sha(leaf) != middle[0]["sha"] or len(leaf) != 1 or leaf[0]["path"] != "journal.json" or leaf[0]["mode"] != "100644" or leaf[0]["type"] != "blob":
            raise RecoveryError("recovery journal tree contains unexpected paths")
        blob = leaf[0]["object"]
        size = blob.get("byteSize") if isinstance(blob, dict) else None
        if type(size) is not int or not 0 <= size <= _MAX_SNAPSHOT_BYTES:
            raise RecoveryError("recovery journal blob exceeds its size bound or is malformed")
        return leaf[0]["sha"], size

    def _graph_history(self, tip):
        owner, name = self.client.repo.split("/", 1)
        after = None
        cursors = set()
        count = 0
        expected = tip
        while True:
            data = self._graph(_HISTORY_QUERY, {"owner": owner, "name": name, "expression": tip, "after": after})
            repo = data.get("repository")
            target = repo.get("object") if isinstance(repo, dict) else None
            payload = target.get("history") if isinstance(target, dict) and target.get("oid") == tip else None
            if not isinstance(payload, dict) or not isinstance(payload.get("nodes"), list) or not isinstance(payload.get("pageInfo"), dict):
                raise RecoveryError("recovery journal history is unavailable or malformed")
            page, info = payload["nodes"], payload["pageInfo"]
            more = info.get("hasNextPage")
            if type(more) is not bool or not page or len(page) > 100 or count + len(page) > _MAX_HISTORY:
                raise RecoveryError("recovery journal history is unavailable or exceeds its safety bound")
            cursor = info.get("endCursor")
            if more and (not isinstance(cursor, str) or not cursor or cursor in cursors):
                raise RecoveryError("recovery journal history pagination is malformed")
            for index, node in enumerate(page):
                if not isinstance(node, dict) or not _is_sha(node.get("oid")) or node["oid"] != expected or node.get("message") not in (_INITIAL_MESSAGE, _UPDATE_MESSAGE):
                    raise RecoveryError("recovery journal ancestry is discontinuous or malformed")
                parents = node.get("parents")
                rows = parents.get("nodes") if isinstance(parents, dict) else None
                parent_info = parents.get("pageInfo") if isinstance(parents, dict) else None
                if not isinstance(rows, list) or len(rows) > 1 or not isinstance(parent_info, dict) or parent_info.get("hasNextPage") is not False:
                    raise RecoveryError("recovery journal ancestry is not single-parent")
                parent = rows[0].get("oid") if rows and isinstance(rows[0], dict) else None
                if rows and not _is_sha(parent):
                    raise RecoveryError("recovery journal parent is malformed")
                pointer, size = self._graph_snapshot_pointer(node.get("tree"))
                expected = parent
                count += 1
                yield node["oid"], parent, pointer, size, index == len(page) - 1 and not more
            if not more:
                return
            cursors.add(cursor)
            after = cursor

    def _blob_bytes(self, sha):
        status, data = self._call("GET", self._path(f"/git/blobs/{sha}"))
        if status != 200 or not isinstance(data, dict) or data.get("sha") != sha or data.get("encoding") != "base64" or not isinstance(data.get("content"), str):
            raise RecoveryError("recovery journal blob is unavailable or malformed")
        try:
            encoded = b"".join(data["content"].encode("ascii").split())
            if len(encoded) > ((_MAX_SNAPSHOT_BYTES + 2) // 3) * 4:
                raise ValueError()
            return base64.b64decode(encoded, validate=True)
        except (ValueError, UnicodeError):
            raise RecoveryError("recovery journal blob is malformed") from None

    def _graph_blobs(self, batch):
        owner, name = self.client.repo.split("/", 1)
        declarations = ", ".join(f"$id{i}: String!" for i in range(len(batch)))
        fields = " ".join(f"b{i}: object(expression: $id{i}) {{ ... on Blob {{ oid byteSize isTruncated text }} }}" for i in range(len(batch)))
        query = f"query RecoveryBlobs($owner: String!, $name: String!, {declarations}) {{ repository(owner: $owner, name: $name) {{ {fields} }} }}"
        variables = {"owner": owner, "name": name, **{f"id{i}": item[2] for i, item in enumerate(batch)}}
        repo = self._graph(query, variables).get("repository")
        if not isinstance(repo, dict) or set(repo) != {f"b{i}" for i in range(len(batch))}:
            raise RecoveryError("recovery journal blobs are unavailable or incomplete")
        for index, (_commit, _parent, sha, size, _last) in enumerate(batch):
            blob = repo[f"b{index}"]
            if not isinstance(blob, dict) or blob.get("oid") != sha or type(blob.get("byteSize")) is not int or blob["byteSize"] != size or type(blob.get("isTruncated")) is not bool:
                raise RecoveryError("recovery journal blob is unavailable or malformed")
            if blob["isTruncated"]:
                raw = self._blob_bytes(sha)
            elif isinstance(blob.get("text"), str):
                raw = blob["text"].encode("utf-8")
            else:
                raise RecoveryError("recovery journal blob is unavailable or malformed")
            if len(raw) != size or len(raw) > _MAX_SNAPSHOT_BYTES or _object_sha("blob", raw) != sha:
                raise RecoveryError("recovery journal blob does not verify")
            try:
                value = _decode_json(raw.decode("utf-8"))
            except (ValueError, UnicodeError):
                raise RecoveryError("recovery journal blob is malformed") from None
            yield _snapshot(value, self.activation["repository_id"])

    @staticmethod
    def _history_transition(parent, child):
        if child["revision"] != parent["revision"] + 1:
            raise RecoveryError("recovery journal revisions are discontinuous")
        parent_records, child_records = parent["records"], child["records"]
        if not set(parent_records).issubset(child_records):
            raise RecoveryError("recovery journal removed a prior record")
        immutable = ("producer", "previous", "source_key", "generation", "submission_id", "intent", "request",
                     "request_digest", "observation")
        allowed = {
            "prepared": {"send-claimed", "confirmed", "superseded"},
            "send-claimed": {"confirmed", "rejected", "recovery-needed", "superseded"},
            "recovery-needed": {"confirmed", "superseded"},
            "rejected": {"confirmed", "superseded"},
            "confirmed": set(),
            "superseded": set(),
        }
        for key, old in parent_records.items():
            new = child_records[key]
            for field in immutable:
                if field in old and new.get(field) != old[field]:
                    raise RecoveryError("recovery journal changed a record identity")
            for field in ("send_nonce", "issue"):
                if old.get(field) is not None and new.get(field) != old[field]:
                    raise RecoveryError("recovery journal changed a consumed record identity")
            old_state, new_state = old.get("state"), new.get("state")
            if old_state not in allowed or new_state not in allowed:
                raise RecoveryError("recovery journal contains an unknown lifecycle state")
            if old_state == new_state:
                if old == new:
                    continue
                # Closure is the sole observation that may enrich a terminal
                # confirmed record without reopening or replacing it.
                if (old_state == "confirmed"
                        and set(old).union(new) == set(old).union({"closed_observation"})
                        and all(old.get(field) == new.get(field) for field in set(old).union(new) - {"closed_observation"})
                        and old.get("closed_observation") is None
                        and new.get("closed_observation") is not None):
                    continue
                raise RecoveryError("recovery journal made a non-monotonic same-state update")
            if new_state not in allowed[old_state]:
                raise RecoveryError("recovery journal made a non-monotonic state transition")

    def _verify_chain(self, tip):
        # Retain only one bounded payload batch, the tip and the adjacent older
        # snapshot. A verified in-process prefix is reusable only after a fresh
        # remote identity/ref read; no cache is persisted or used as a permit.
        first = child = None
        count = 0
        batch = []
        batch_bytes = 0

        def consume():
            nonlocal first, child, count, batch, batch_bytes
            for snapshot in self._graph_blobs(batch):
                if first is None:
                    first = snapshot
                if child is not None:
                    self._history_transition(snapshot, child)
                child = snapshot
                count += 1
            batch, batch_bytes = [], 0

        for entry in self._graph_history(tip):
            sha, parent, _pointer, size, last = entry
            if sha == self._last_tip and self._last_snapshot is not None:
                if batch:
                    consume()
                if child is not None:
                    self._history_transition(self._last_snapshot, child)
                if count + self._last_depth > _MAX_HISTORY:
                    raise RecoveryError("recovery journal history exceeds its safety bound")
                return first or copy.deepcopy(self._last_snapshot), count + self._last_depth
            if batch and (len(batch) >= _GRAPH_BATCH or batch_bytes + size > _GRAPH_BYTES):
                consume()
            batch.append(entry)
            batch_bytes += size
            if sha == self.activation["genesis"]:
                if parent is not None or not last:
                    raise RecoveryError("recovery journal genesis has unexpected ancestry")
                consume()
                if child != _initial(self.activation["repository_id"]):
                    raise RecoveryError("recovery journal genesis does not match activation")
                if self._last_tip is not None:
                    raise RecoveryError("recovery journal appears to have been rewound")
                return first, count
            if parent is None or last:
                raise RecoveryError("recovery journal genesis is missing from history")
        raise RecoveryError("recovery journal history is incomplete")

    def load(self):
        self._repo_id()
        tip = self._ref()
        if tip != self._last_tip or self._last_snapshot is None:
            snapshot, depth = self._verify_chain(tip)
            self._last_tip = tip
            self._last_snapshot = copy.deepcopy(snapshot)
            self._last_depth = depth
        return tip, copy.deepcopy(self._last_snapshot)

    def _post_blob(self, raw):
        expected = _object_sha("blob", raw)
        body = {"content": base64.b64encode(raw).decode("ascii"), "encoding": "base64"}
        status, data = self._call("POST", self._path("/git/blobs"), body)
        if status not in (200, 201) or not isinstance(data, dict) or data.get("sha") != expected:
            raise RecoveryError("cannot write recovery journal blob")
        return expected

    def _post_tree(self, entries):
        expected = _tree_sha(entries)
        status, data = self._call("POST", self._path("/git/trees"), {"tree": entries})
        if status not in (200, 201) or not isinstance(data, dict) or data.get("sha") != expected:
            raise RecoveryError("cannot write recovery journal tree")
        return expected

    def _write_tree(self, snapshot):
        raw = _canonical(_snapshot(snapshot, self.activation["repository_id"]))
        blob = self._post_blob(raw)
        recovery = self._post_tree([{"path": "journal.json", "mode": "100644", "type": "blob", "sha": blob}])
        engine = self._post_tree([{"path": "issue-recovery", "mode": "040000", "type": "tree", "sha": recovery}])
        return self._post_tree([{"path": ".engine", "mode": "040000", "type": "tree", "sha": engine}])

    def _post_commit(self, tree, parents, message):
        status, data = self._call("POST", self._path("/git/commits"), {"message": message, "tree": tree, "parents": parents})
        sha = data.get("sha") if isinstance(data, dict) else None
        response_tree = data.get("tree", {}).get("sha") if isinstance(data, dict) and isinstance(data.get("tree"), dict) else None
        response_parents = data.get("parents") if isinstance(data, dict) else None
        parent_shas = [item.get("sha") for item in response_parents] if isinstance(response_parents, list) and all(isinstance(item, dict) for item in response_parents) else None
        if status not in (200, 201) or not _is_sha(sha) or response_tree != tree or parent_shas != parents:
            raise RecoveryError("cannot write recovery journal commit")
        return sha

    def compare_and_swap(self, expected_tip, snapshot):
        if not _is_sha(expected_tip):
            raise RecoveryError("expected recovery journal tip is malformed")
        _snapshot(snapshot, self.activation["repository_id"])
        # Verify the current remote identity/ref and any unverified suffix.
        # A matching immutable prefix was fully checked by this instance.
        actual, _parent_snapshot = self.load()
        if actual != expected_tip:
            raise Conflict("recovery journal advanced; reload before deciding again")
        self._history_transition(_parent_snapshot, snapshot)
        if self._last_depth >= _MAX_HISTORY:
            raise RecoveryError("recovery journal history exceeds its safety bound")
        root = self._write_tree(snapshot)
        commit = self._post_commit(root, [expected_tip], _UPDATE_MESSAGE)
        status, data = self._call("PATCH", self._path("/git/refs/heads/codex/engine-issue-recovery"), {"sha": commit, "force": False})
        # An absent/invalid PATCH response is ambiguous.  Do not read it back and turn
        # a possibly successful write into permission to send.
        returned = data.get("object", {}).get("sha") if isinstance(data, dict) and isinstance(data.get("object"), dict) else None
        if status not in (200, 201) or not _is_sha(returned):
            if status in (409, 422):
                raise Conflict("recovery journal advance conflicted; reload before deciding again")
            raise RecoveryError("recovery journal advance is unavailable or ambiguous")
        if returned != commit:
            raise RecoveryError("recovery journal advance returned an unexpected ref")
        if self._ref() != commit:
            raise Conflict("recovery journal changed before readback")
        self._last_tip = commit
        self._last_snapshot = copy.deepcopy(snapshot)
        self._last_depth += 1
        return commit


def initialize(client):
    """Create a new pinned recovery journal; never adopt or repair an existing ref."""
    repo = getattr(client, "repo", None)
    transport = getattr(client, "_transport", None)
    if not isinstance(repo, str) or not _REPO.fullmatch(repo) or not callable(transport):
        raise RecoveryError("recovery store requires a bound GitHub client")

    # A temporary activation only permits the shared request/response helpers; it
    # is replaced with the newly created root commit before any journal read.
    probe = object.__new__(GitStore)
    probe.client = client
    probe.activation = {"repository_id": 1, "genesis": "0" * 40}
    probe._last_tip = None
    status, data = probe._call("GET", probe._path(""))
    repository_id = data.get("id") if status == 200 and isinstance(data, dict) else None
    if not isinstance(repository_id, int) or isinstance(repository_id, bool) or repository_id <= 0:
        raise RecoveryError("cannot verify the recovery repository")
    ref_status, _ref_data = probe._call("GET", probe._path("/git/ref/heads/codex/engine-issue-recovery"))
    if ref_status == 200:
        raise RecoveryError("recovery journal ref already exists")
    if ref_status != 404:
        raise RecoveryError("cannot determine whether the recovery journal ref exists")
    probe.activation = {"repository_id": repository_id, "genesis": "0" * 40}
    root = probe._write_tree(_initial(repository_id))
    commit = probe._post_commit(root, [], _INITIAL_MESSAGE)
    try:
        probe._call("POST", probe._path("/git/refs"), {"ref": REF, "sha": commit})
    except RecoveryError:
        # Genesis creation carries no send permit. Its exact known commit may
        # therefore be verified after response loss; a claim PATCH may not.
        pass
    activation = {"repository_id": repository_id, "genesis": commit}
    verifier = GitStore(client, activation)
    verified_tip, _snapshot_value = verifier.load()
    if verified_tip != commit:
        raise RecoveryError("recovery journal ref readback did not match creation")
    return activation
