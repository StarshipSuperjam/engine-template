"""Equivalence proof for the copy-based review recipe (StarshipSuperjam/engine-template#947, fix c).

#947 replaces the worktree-based way a review agent built a throwaway tree with a copy-based one
(engine_fixture.clone_engine). This test proves the swap costs the reviewer nothing: the engine
surface a copy produces is byte-identical to the one a real `git worktree add` of the same commit
would give — and the copy is strictly safer, because it carries no `.git`, so there is no shared
`.git/config` whose `origin` a stray command could repoint (the incident-2 mechanism).

The reference oracle is a genuine `git worktree add`, run against a throwaway repo the test creates
itself — never the shared checkout — which is exactly the discipline #947 ships.
"""
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_fixture  # noqa: E402


def _git(args, cwd):
    subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, check=True)


def _write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


# A representative engine surface: one file under two COPY_DIRS plus a loose COPY_FILE.
_SURFACE = {
    ".engine/tools/example.py": "print('engine tool')\n",
    ".claude/agents/example-review.md": "---\nname: example-review\n---\n\nbody\n",
    "CLAUDE.md": "# project\n",
}


def _collect(root, rels):
    """{rel: bytes} for each engine-surface path present under root."""
    out = {}
    for rel in rels:
        path = os.path.join(root, rel)
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                out[rel] = fh.read()
    return out


class TestCopyEqualsWorktree(unittest.TestCase):
    def test_copy_reproduces_the_worktree_surface_without_the_git_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            main = os.path.join(tmp, "repo")
            os.makedirs(main)
            _git(["init", "-q"], main)
            _git(["config", "user.email", "t@example.com"], main)
            _git(["config", "user.name", "t"], main)
            for rel, text in _SURFACE.items():
                _write(main, rel, text)
            # an UNTRACKED junk file that must NOT travel into the copy (the whole point of tracked-only)
            _write(main, ".engine/tools/untracked_junk.py", "secret = 1\n")
            _git(["add", ".engine/tools/example.py", ".claude/agents/example-review.md", "CLAUDE.md"], main)
            _git(["commit", "-q", "-m", "surface"], main)

            # Reference oracle: a REAL git worktree of this commit (the old recipe), on a repo we own.
            worktree = os.path.join(tmp, "worktree")
            _git(["worktree", "add", "-q", "--detach", worktree, "HEAD"], main)

            # The new recipe: a tracked-only copy.
            dest = os.path.join(tmp, "copy")
            engine_fixture.clone_engine(main, dest)

            rels = list(_SURFACE)
            worktree_surface = _collect(worktree, rels)
            copy_surface = _collect(dest, rels)

            # 1. Equivalence: the copy reproduces the worktree's engine surface exactly.
            self.assertEqual(copy_surface, worktree_surface,
                             "the copy-based recipe yields the same engine surface a worktree would")
            self.assertEqual(set(copy_surface), set(_SURFACE),
                             "every committed surface file is present in the copy")

            # 2. Safety: the copy carries no .git, so there is no shared config an errant command could
            #    repoint — the structural reason it cannot cause incident 2. The worktree, by contrast,
            #    has a .git pointer into the shared repo (illustrating the hazard the recipe retires).
            self.assertFalse(os.path.exists(os.path.join(dest, ".git")),
                             "the copy has no .git — nothing shared to mutate")
            self.assertTrue(os.path.exists(os.path.join(worktree, ".git")),
                            "the worktree has a .git pointer into the shared repo (the retired hazard)")

            # 3. Tracked-only: untracked junk never travels into the copy.
            self.assertFalse(os.path.exists(os.path.join(dest, ".engine/tools/untracked_junk.py")),
                             "an untracked file is excluded from the copy")

            _git(["worktree", "remove", "--force", worktree], main)  # tidy the throwaway repo we own


if __name__ == "__main__":
    unittest.main()
