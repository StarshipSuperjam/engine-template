#!/usr/bin/env python3
"""The deferred-work marker: its parser, and the `list` command that enumerates every one (eADR-0039).

A marker records, at the site in source, work knowingly left unbuilt. Writing one costs a description and
nothing else — no issue, milestone, or owner reference — because a tracked issue is the escalation for a
marker nobody clears, never the price of recording one.

RECOGNITION IS A FROZEN ON-DISK FORMAT. The parser travels by the engine overlay while markers travel in each
repository's own committed source, so the only possible skew is a new parser meeting old markers. The rule may
therefore only ever WIDEN what it accepts. Narrowing it — including a bug fix that tightens — would redden
committed source across every deployed repository with no migration path.

The trigger is recognised in exactly two positions, and the pair is what makes it correct:
  - first token immediately after the line's first comment leader — this admits a trailing note after code,
    the idiomatic short comment in Python. Anchoring at line start alone missed it, and an author who believes
    a deferral was recorded while nothing can see it is worse off than with the prose it replaced.
  - first non-whitespace token on the line — this admits a docstring or block-comment interior, where most of
    this engine's notes actually sit.
Requiring the trigger to be the FIRST token after the leader (rather than anywhere after it) is what keeps a
heading, an issue citation, or prose naming the form inline from becoming a marker. The markdown bullet
character is deliberately absent from the leader set: it made an ordinary list item a marker.

A surface with no comment syntax carries no marker. A deferral concerning such a file is recorded in the code
that owns the behaviour, naming that file — a contracted limit, not an oversight.

AUTHORING RULE for anything that teaches the form: show it inline, mid-sentence, in backticks — never starting
a line and never directly after a comment leader. That one rule is why this file needs no exclusion entry for
prose, and `list` returning nothing on a tree with no real markers is what verifies it.
"""
from __future__ import annotations
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402

TOKEN = "ENGINE-TODO"

# The trigger: the token, then either a bare colon or a parenthesised issue reference and a colon. Written so
# this line is not itself an instance — the character after the token here is an escape, never a colon.
TRIGGER = re.compile(re.escape(TOKEN) + r"(?:\(#(\d+)\))?:")

# Comment leaders, longest-first so `//` is preferred over a bare `/` and `<!--` over `--`. The markdown
# bullet is excluded by design (see the module note).
LEADERS = ("<!--", "//", "/*", "--", "#")

_SKIP_DIR_NAMES = frozenset({".git"})

# The one structural exclusion: the negative-fixture tree, whose markers are deliberately malformed so this
# check can be witnessed biting them. It is computed RELATIVE TO THE SCAN BASE, which is what lets the fixture
# run scan itself — a prune tested against an absolute path containing the fixture directory would make the
# fixture prune itself and the meta-check would pass while proving nothing. Nothing else is excluded: tests
# and demos are ordinary source, and a marker in one is as real as any other.
_FIXTURE_PREFIX = ".engine/_fixtures/"


class Marker:
    """One recognised marker: where it is, the issue it names if any, and what it says."""

    __slots__ = ("path", "line", "ref", "description")

    def __init__(self, path, line, ref, description):
        self.path, self.line, self.ref, self.description = path, line, ref, description

    def as_dict(self) -> dict:
        return {"path": self.path, "line": self.line, "ref": self.ref, "description": self.description}


def _leader_at(line: str):
    """The line's FIRST comment-leader occurrence as (index, leader), or None. Only the first counts: a
    leader appearing later on the line is inside the first comment's own text, not a second comment."""
    best = None
    for leader in LEADERS:
        i = line.find(leader)
        if i != -1 and (best is None or i < best[0]):
            best = (i, leader)
    return best


def recognise(line: str):
    """The trigger's match on `line` if it sits in a recognised position, else None.

    Returns (match, leader, leader_col) — leader is None when the line-start position matched, which is what
    the continuation rule keys on to tell a commented marker from a docstring one."""
    stripped = line.lstrip()
    m = TRIGGER.match(stripped)
    if m:                                    # first non-whitespace token on the line
        return m, None, len(line) - len(stripped)
    found = _leader_at(line)
    if found is not None:
        index, leader = found
        rest = line[index + len(leader):]
        m = TRIGGER.match(rest.lstrip())
        if m:                                # first token immediately after the first leader
            return m, leader, index
    return None


def _tail(line: str, m, leader) -> str:
    """The description text on the marker's own line: everything after the trigger, with a trailing
    block-comment closer removed so a one-line HTML or C comment does not end up inside the description."""
    if leader is None:
        text = line.lstrip()[m.end():]
    else:
        index, _ = _leader_at(line)
        rest = line[index + len(leader):]
        text = rest.lstrip()[m.end():]
    for closer in ("-->", "*/"):
        if text.rstrip().endswith(closer):
            text = text.rstrip()[: -len(closer)]
    return text.strip()


def _continues(line: str, leader, leader_col: int, indent: int) -> "str | None":
    """The continuation text `line` contributes to an open marker, or None if it closes the marker.

    A commented marker continues on lines carrying the SAME leader at a column at or right of the marker's; a
    leaderless (docstring) marker continues on lines indented strictly deeper than the marker itself. Either
    way a blank line, a line that fails the test, or a line carrying its own trigger closes it — so an older
    parser meeting a newer multi-line marker still reads a description that is truncated but never wrong."""
    if not line.strip():
        return None
    if recognise(line) is not None:
        return None
    if leader is None:
        this_indent = len(line) - len(line.lstrip())
        return line.strip() if this_indent > indent else None
    index = line.find(leader)
    if index == -1 or index < leader_col:
        return None
    text = line[index + len(leader):].strip()
    for closer in ("-->", "*/"):
        if text.endswith(closer):
            text = text[: -len(closer)].strip()
    return text or None


def scan_text(text: str, path: str = "") -> list:
    """Every marker in `text`, descriptions joined across their continuation lines."""
    out, lines = [], text.splitlines()
    i = 0
    while i < len(lines):
        got = recognise(lines[i])
        if got is None:
            i += 1
            continue
        m, leader, col = got
        parts = [_tail(lines[i], m, leader)]
        start, indent = i + 1, col
        j = i + 1
        while j < len(lines):
            more = _continues(lines[j], leader, col, indent)
            if more is None:
                break
            parts.append(more)
            j += 1
        out.append(Marker(path, i + 1, ("#" + m.group(1)) if m.group(1) else None,
                          " ".join(p for p in parts if p).strip()))
        i = j
    return out


def scan_file(path: str, rel: str = "") -> list:
    """Every marker in one file. An unreadable or non-text file yields none rather than raising — a marker
    scan must never be the thing that fails a run."""
    try:
        text = validate.read(path)
    except (OSError, UnicodeDecodeError):
        return []
    return scan_text(text, rel or path)


def tracked_files(root: str = None) -> list:
    """The repository's git-tracked files, repo-relative. Tracked enumeration rather than a filesystem walk
    is what keeps a vendored dependency tree, a virtualenv, or a build output directory out of the scan
    without maintaining a prune list — the index already answers that question. Reads the index, never
    history, so it stays offline and unaffected by a shallow checkout."""
    base = root or validate.ROOT
    try:
        done = subprocess.run(["git", "ls-files", "-z"], cwd=base, capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    if done.returncode != 0:
        return []
    return [p for p in done.stdout.decode("utf-8", "replace").split("\0")
            if p and os.path.basename(os.path.dirname(p)) not in _SKIP_DIR_NAMES]


def walked_files(root: str) -> list:
    """Every file under `root`, repo-relative — the enumeration for a SEEDED TREE, never for a repository.

    A real repository is enumerated from the git index (`tracked_files`), which is what keeps a vendored
    dependency tree, a virtualenv or a build directory out of the scan for free. A seeded fixture tree has no
    index of its own, and falling back to an empty list there would let the negative-fixture meta-check pass
    while the check did nothing — the exact vacuous green that meta-check exists to catch."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES]
        for name in sorted(filenames):
            out.append(os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/"))
    return sorted(out)


def markers(root: str = None, skip: "set | None" = None, walk: bool = False) -> list:
    """Every marker across the tree, sorted by path then line. `skip` is a set of repo-relative paths the
    caller wants left out; the parser itself has no opinion about ownership. `walk` enumerates the filesystem
    instead of the git index and is for a seeded tree only."""
    base = root or validate.ROOT
    skip = skip or set()
    out = []
    for rel in (walked_files(base) if walk else tracked_files(base)):
        if rel in skip or rel.startswith(_FIXTURE_PREFIX):
            continue
        out.extend(scan_file(os.path.join(base, rel), rel))
    out.sort(key=lambda t: (t.path, t.line))
    return out


def engine_owned_skip(root: str = None) -> set:
    """The paths to leave out in a DEPLOYED repository: the files an engine update overwrites, because a local
    fix to one of those is wiped on the next update, so surfacing a marker there asks the operator for
    something they cannot deliver. Empty in the engine's own home repository, where those files are the work.

    The deployed test fails toward RUNNING rather than skipping: a checkout whose origin cannot be read reports
    everything, because an under-count that reads as clean is the worse failure. Any error leaves the set
    empty, which likewise shows more rather than less."""
    base = root or validate.ROOT
    try:
        import repo_identity
        if not repo_identity.is_downstream_copy_strict(base):
            return set()
        import module_manager
        return set(module_manager.overlay_replace_paths())
    except Exception:      # noqa: BLE001 — degrade toward showing everything, never toward a silent under-count
        return set()


def _cmd_list(argv: list) -> int:
    as_json = "--json" in argv
    skip = engine_owned_skip()
    found = markers(skip=skip)
    if as_json:
        print(json.dumps([t.as_dict() for t in found], indent=2))
        return 0
    if not found:
        print("No outstanding deferred-work markers.")
    for t in found:
        ref = f" {t.ref}" if t.ref else ""
        print(f"{t.path}:{t.line}{ref}  {t.description}")
    if found:
        print(f"\n{len(found)} outstanding.")
    if skip:
        # Disclosed, never silent: an operator seeing a short list should know why it is short.
        print(f"({len(skip)} engine-owned files were not scanned — an engine update overwrites them, so a "
              f"marker there is not yours to clear.)")
    return 0


# The demo's seeded input: real source text through the real parser. Assembled from parts so this file
# carries no instance of the trigger itself — the same authoring rule every other surface keeps.
_T = TOKEN + ":"
_R = TOKEN + "(#412):"
_DEMO_SOURCE = "\n".join([
    'def write(record):',
    '    """Append one record.',
    '',
    f'    {_T} the version envelope is not written; every record carries format 1 today.',
    '        Callers that need a shape version read it from the ledger header instead.',
    '',
    '    Returns the offset written."""',
    '    return _append(record)                    ' + "# " + _R + ' no retry path yet; a failed write raises.',
    '',
    '# A heading, a citation and an inline mention must all stay invisible to the scan:',
    '## Writing an ' + _T + ' marker',
    '# Issue #412 tracks the ' + _T + ' grammar',
    'MESSAGE = "' + _T + ' this is a string literal, not a marker"',
    '* ' + _T + ' a markdown bullet is not a comment leader',
])


def _demo(argv: list) -> int:
    print("DEFERRED-WORK MARKER DEMO — real source text, real parser, no fakes.\n")
    for n, line in enumerate(_DEMO_SOURCE.splitlines(), 1):
        print(f"  {n:>2}| {line}")
    found = scan_text(_DEMO_SOURCE, "demo_source.py")
    print(f"\nRecognised {len(found)} marker(s):\n")
    for t in found:
        print(f"  line {t.line}  ref={t.ref}\n    {t.description}\n")

    failures = []
    if len(found) != 2:
        failures.append(f"expected exactly 2 markers (the docstring one and the trailing comment), got {len(found)}")
    if len(found) == 2:
        doc, trailing = found
        if doc.ref is not None:
            failures.append("the docstring marker carries no issue reference and must report ref=None")
        if "ledger header" not in doc.description:
            failures.append("the docstring marker must join its indented continuation line into the description")
        if trailing.ref != "#412":
            failures.append(f"the trailing-comment marker must report ref '#412', got {trailing.ref!r}")
        if "retry path" not in trailing.description:
            failures.append("a marker written as a trailing comment after code must be recognised")
    for bad in ("Writing an", "Issue #412 tracks", "string literal", "markdown bullet"):
        if any(bad in t.description for t in found):
            failures.append(f"a line containing {bad!r} was wrongly recognised as a marker")

    if failures:
        print("DEMO FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS — the docstring marker and the trailing-comment marker were both recognised; the heading, the\n"
          "issue citation, the string literal and the markdown bullet were all correctly ignored.")
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "list":
        return _cmd_list(argv[1:])
    if argv and argv[0] == "demo":
        return _demo(argv[1:])
    print("usage: engine_todo.py list [--json] | demo", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
