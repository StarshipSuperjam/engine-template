#!/usr/bin/env python3
"""Accepted helper that gives explicitly selected candidate code only one disposable target."""
from __future__ import annotations

import argparse
import importlib.util
import os
import runpy
import stat
import sys


_CONTEXT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "execution_context.py")
_CONTEXT_SPEC = importlib.util.spec_from_file_location("_engine_candidate_invocation_context", _CONTEXT_PATH)
if _CONTEXT_SPEC is None or _CONTEXT_SPEC.loader is None:  # pragma: no cover - a malformed accepted tree
    raise RuntimeError("accepted candidate context authority is unavailable")
execution_context = importlib.util.module_from_spec(_CONTEXT_SPEC)
sys.modules[_CONTEXT_SPEC.name] = execution_context
_CONTEXT_SPEC.loader.exec_module(execution_context)


class CandidateInvocationError(RuntimeError):
    pass


def _inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath((path, root)) == root
    except ValueError:
        return False


def run(args: argparse.Namespace) -> int:
    context = execution_context.current_context()
    document = context.to_document()
    if document["target"]["kind"] != "disposable":
        raise CandidateInvocationError("candidate invocation received canonical authority")
    target_root = os.path.realpath(args.target_root)
    if not os.path.isabs(args.target_root) or os.path.abspath(args.target_root) != args.target_root \
            or target_root != args.target_root:
        raise CandidateInvocationError("candidate target is not one normalized non-link absolute path")
    if target_root != os.path.dirname(document["target"]["memory_dir"]):
        raise CandidateInvocationError("candidate invocation target differs from its sealed context")
    candidate_root = os.path.realpath(args.candidate_root)
    if not os.path.isabs(args.candidate_root) or os.path.abspath(args.candidate_root) != args.candidate_root \
            or candidate_root != args.candidate_root:
        raise CandidateInvocationError("candidate root is not one normalized non-link absolute path")
    tools_root = os.path.join(candidate_root, ".engine", "tools")
    memory_tools = os.path.join(tools_root, "memory")
    script = os.path.realpath(args.script)
    try:
        script_info = os.lstat(args.script)
    except OSError as exc:
        raise CandidateInvocationError("candidate script is absent or unreadable") from exc
    if (not os.path.isabs(args.script) or os.path.abspath(args.script) != args.script
            or script != args.script or stat.S_ISLNK(script_info.st_mode)
            or not stat.S_ISREG(script_info.st_mode) or not _inside(script, memory_tools)):
        raise CandidateInvocationError("candidate script is outside the selected candidate memory tools")
    sys.path[:] = [tools_root, *[
        os.path.realpath(path) for path in args.site_path if os.path.isdir(path)
    ], *[path for path in sys.path if isinstance(path, str) and path and path != tools_root]]
    import validate
    if not _inside(os.path.realpath(validate.__file__), tools_root):
        raise CandidateInvocationError("candidate validate module escaped the selected code root")
    validate.ROOT = target_root
    os.environ.update({
        "ENGINE_MEMORY_DIR": document["target"]["memory_dir"],
        "ENGINE_PROJECT_ROOT": target_root,
        "ENGINE_CANDIDATE_DISPOSABLE": "1",
    })
    old_argv, old_cwd = sys.argv, os.getcwd()
    sys.argv = [script, *args.target_args]
    os.chdir(target_root)
    try:
        runpy.run_path(script, run_name="__main__")
    except SystemExit as exc:
        if exc.code is None:
            return 0
        if isinstance(exc.code, int):
            return exc.code
        print(exc.code, file=sys.stderr)
        return 1
    finally:
        sys.argv = old_argv
        os.chdir(old_cwd)
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--candidate-root", required=True)
    result.add_argument("--script", required=True)
    result.add_argument("--target-root", required=True)
    result.add_argument("--site-path", action="append", default=[])
    result.add_argument("target_args", nargs=argparse.REMAINDER)
    return result


def main(argv: list[str]) -> int:
    args = parser().parse_args(argv)
    if args.target_args and args.target_args[0] == "--":
        args.target_args = args.target_args[1:]
    try:
        return run(args)
    except (CandidateInvocationError, execution_context.ContextError) as exc:
        print(f"candidate invocation refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
