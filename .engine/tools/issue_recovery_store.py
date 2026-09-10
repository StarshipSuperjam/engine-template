"""Git-data backing store for durable Engine issue-recovery journals.

This module deliberately knows only enough about a journal to keep its Git
history and immutable record identities safe.  ``issue_recovery`` owns the
closed lifecycle schema and state transitions.  The adapter has no credential
or GitHub-client construction path: callers provide the already-bound client.
"""

from __future__ import annotations

import base64
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

    def _get_commit(self, sha):
        status, data = self._call("GET", self._path(f"/git/commits/{sha}"))
        if status != 200 or not isinstance(data, dict) or not _is_sha(data.get("sha")) or data["sha"] != sha:
            raise RecoveryError("recovery journal commit is unavailable or malformed")
        tree = data.get("tree")
        parents = data.get("parents")
        if not isinstance(tree, dict) or not _is_sha(tree.get("sha")) or not isinstance(parents, list):
            raise RecoveryError("recovery journal commit is malformed")
        parent_shas = []
        for parent in parents:
            parent_sha = parent.get("sha") if isinstance(parent, dict) else None
            if not _is_sha(parent_sha):
                raise RecoveryError("recovery journal commit is malformed")
            parent_shas.append(parent_sha)
        message = data.get("message")
        if message not in (_INITIAL_MESSAGE, _UPDATE_MESSAGE):
            raise RecoveryError("recovery journal commit has unexpected metadata")
        return tree["sha"], parent_shas

    def _tree_entries(self, sha):
        status, data = self._call("GET", self._path(f"/git/trees/{sha}"))
        if status != 200 or not isinstance(data, dict) or data.get("sha") != sha or data.get("truncated") is True:
            raise RecoveryError("recovery journal tree is unavailable or malformed")
        entries = data.get("tree")
        if not isinstance(entries, list):
            raise RecoveryError("recovery journal tree is malformed")
        normalized = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise RecoveryError("recovery journal tree is malformed")
            path, mode, kind, entry_sha = entry.get("path"), entry.get("mode"), entry.get("type"), entry.get("sha")
            if not isinstance(path, str) or not isinstance(mode, str) or not isinstance(kind, str) or not _is_sha(entry_sha):
                raise RecoveryError("recovery journal tree is malformed")
            normalized.append({"path": path, "mode": mode, "type": kind, "sha": entry_sha})
        if _tree_sha(normalized) != sha:
            raise RecoveryError("recovery journal tree hash does not verify")
        return normalized

    def _only(self, sha, path, mode, kind):
        entries = self._tree_entries(sha)
        if len(entries) != 1:
            raise RecoveryError("recovery journal tree contains unexpected paths")
        entry = entries[0]
        if entry["path"] != path or entry["mode"] != mode or entry["type"] != kind:
            raise RecoveryError("recovery journal tree contains unexpected paths")
        return entry["sha"]

    def _read_snapshot(self, tree_sha):
        engine = self._only(tree_sha, ".engine", "040000", "tree")
        recovery = self._only(engine, "issue-recovery", "040000", "tree")
        blob_sha = self._only(recovery, "journal.json", "100644", "blob")
        status, data = self._call("GET", self._path(f"/git/blobs/{blob_sha}"))
        if status != 200 or not isinstance(data, dict) or data.get("sha") != blob_sha or data.get("encoding") != "base64" or not isinstance(data.get("content"), str):
            raise RecoveryError("recovery journal blob is unavailable or malformed")
        try:
            # GitHub may fold a base64 blob with line breaks.  Accept that wire
            # formatting, but reject all non-base64 characters after unfolding.
            encoded = b"".join(data["content"].encode("ascii").split())
            raw = base64.b64decode(encoded, validate=True)
            value = _decode_json(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, UnicodeEncodeError, json.JSONDecodeError) as exc:
            raise RecoveryError("recovery journal blob is malformed") from exc
        if len(raw) > _MAX_SNAPSHOT_BYTES or _object_sha("blob", raw) != blob_sha:
            raise RecoveryError("recovery journal blob does not verify")
        return _snapshot(value, self.activation["repository_id"])

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
        snapshots = []
        sha = tip
        for _ in range(_MAX_HISTORY):
            tree, parents = self._get_commit(sha)
            snapshot = self._read_snapshot(tree)
            snapshots.append((sha, snapshot, parents))
            if sha == self.activation["genesis"]:
                if parents or snapshot != _initial(self.activation["repository_id"]):
                    raise RecoveryError("recovery journal genesis does not match activation")
                break
            if len(parents) != 1:
                raise RecoveryError("recovery journal ancestry is not single-parent")
            sha = parents[0]
        else:
            raise RecoveryError("recovery journal history exceeds its safety bound")
        for index in range(len(snapshots) - 1, 0, -1):
            self._history_transition(snapshots[index][1], snapshots[index - 1][1])
        return snapshots[0][1]

    def load(self):
        self._repo_id()
        tip = self._ref()
        if self._last_tip is not None and tip != self._last_tip:
            # A different tip is acceptable only when it extends the previously verified one.
            # The chain verification below proves that relation by locating the old tip.
            current = tip
            seen = False
            for _ in range(_MAX_HISTORY):
                if current == self._last_tip:
                    seen = True
                    break
                _tree, parents = self._get_commit(current)
                if not parents:
                    break
                if len(parents) != 1:
                    break
                current = parents[0]
            if not seen:
                raise RecoveryError("recovery journal appears to have been rewound")
        snapshot = self._verify_chain(tip)
        self._last_tip = tip
        return tip, snapshot

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
        # This is deliberately a full read, not merely a ref comparison: an
        # otherwise matching parent must still have valid anchored history and
        # lifecycle transitions before we append to it.
        actual, _parent_snapshot = self.load()
        if actual != expected_tip:
            raise Conflict("recovery journal advanced; reload before deciding again")
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
    status, data = probe._call("POST", probe._path("/git/refs"), {"ref": REF, "sha": commit})
    returned = data.get("object", {}).get("sha") if isinstance(data, dict) and isinstance(data.get("object"), dict) else None
    if status not in (200, 201) or returned != commit:
        raise RecoveryError("recovery journal ref creation is unavailable or ambiguous")
    activation = {"repository_id": repository_id, "genesis": commit}
    verifier = GitStore(client, activation)
    if verifier._ref() != commit:
        raise RecoveryError("recovery journal ref readback did not match creation")
    verifier._verify_chain(commit)
    return activation
