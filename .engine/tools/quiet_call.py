#!/usr/bin/env python3
"""Run a callable with its stdout captured — the test-support helper that keeps a demo walkthrough
from burying the `Ran N tests … OK` summary when the suite is run without unittest's `-b`.

Why this exists: a handful of `test_*.py` self-tests call a `demo_*` module's functions — `main()` to
attest the happy path exits 0, and helper legs like `_scan_planted_secret()` to attest a real-binary
behavior. They assert the RETURN VALUE (an exit code, a bool), never the printed walkthrough — but each
of those demo functions prints. Run through `unittest discover` WITHOUT `-b`, those lines flood the tail
and hide the pass/fail summary, so the run has to be repeated (with `-b`) just to read a clean result.
`-b` is the CI default and the documented fix, but a session that reconstructs the command by habit
drops the flag and floods again — the recurring papercut this helper removes.

Capturing the demo's stdout AT THE CALL SITE makes the first run clean on ANY invocation — buffered or
not, `discover` or single-file — so the flag is no longer load-bearing for a legible summary. (`-b`
stays the CI default as general belt-and-suspenders; this just stops a dropped flag from mattering.)

Pass the demo function as a REFERENCE — `quiet_call.run(demo_x.some_fn)` — never a call
(`demo_x.some_fn()`): a direct call prints before the helper can capture it. The durability guard in
test_quiet_call.py fails the suite if any `test_*.py` calls a demo function directly again, so the
clean-first-run property is enforced, not just cleaned up once.

stdout only: stderr is left alone, so a demo's failure diagnostic — and its non-zero return — still
reach the runner; only the success-path walkthrough is captured and discarded.
"""
from __future__ import annotations

import contextlib
import io
from typing import Callable, TypeVar

_T = TypeVar("_T")


def run(fn: Callable[..., _T], *args, **kwargs) -> _T:
    """Call `fn(*args, **kwargs)` with its stdout captured, returning whatever it returns (an exit
    code, a bool — the caller asserts on that, not on the printed walkthrough, which is discarded). An
    exception propagates — `redirect_stdout` restores stdout and re-raises on the way out — so a
    crashing demo still fails loudly rather than being swallowed."""
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args, **kwargs)
