#!/bin/sh
# hook-runner.sh — the engine's hook launcher.
#
# Every engine hook registered in .claude/settings.json runs through this one script instead of
# inlining a wall of shell in each registration. The command Claude Code DISPLAYS after a hook fires
# is the registration's command string verbatim, so collapsing the preamble here keeps that display
# short and legible to the non-engineer operator (it otherwise reads as a wall of code / an error).
#
# The behavior is exactly the old inline preamble's, unchanged:
#   - a BOUNDED wait for the engine tool-runtime interpreter to appear — the fresh-worktree race
#     (issue #83): the gitignored .engine/.venv is provisioned a beat after a checkout, so a hook that
#     fires in that window finds no interpreter; the wait polls for it, then execs it;
#   - it NEVER falls back to the operator's system Python — if the interpreter never appears within the
#     bound, it runs NOTHING (constraints: the engine "cannot manage a language runtime").
#
# Usage (this exact form is rendered by hooks.hook_command, so it is byte-pinned by a drift test):
#   sh hook-runner.sh <venv-interpreter> <script> [args...]
#     $1        the explicit ${CLAUDE_PROJECT_DIR}-rooted venv interpreter, resolved per-OS (POSIX
#               bin/python, Windows Scripts/python.exe). It is named in the command string itself, so
#               D-156's "the hook command names the interpreter explicitly" stays mechanically
#               witnessable in the diff, not hidden in this file.
#     $2 .. $#  the hook script (${CLAUDE_PROJECT_DIR}-rooted) and any trailing args (e.g. `hook`).
#
# The wait ceiling is 50 polls x 0.1s (~5s); ENGINE_HOOK_WAIT_POLLS / ENGINE_HOOK_WAIT_INTERVAL override
# it (the tests shrink it so they stay fast). Invoked as `sh hook-runner.sh ...`, so it needs no
# executable bit and travels via "Use this template" like every other committed engine file.
interp="$1"
shift
polls="${ENGINE_HOOK_WAIT_POLLS:-50}"
interval="${ENGINE_HOOK_WAIT_INTERVAL:-0.1}"
n=0
while [ ! -x "$interp" ] && [ "$n" -lt "$polls" ]; do
    sleep "$interval"
    n=$((n + 1))
done
if [ -x "$interp" ]; then
    exec "$interp" "$@"
fi
# The engine's private Python runtime never appeared within the wait bound (issue #83's fresh-worktree
# window elapsed, or .engine/.venv is not provisioned). There is no interpreter here to emit a structured
# finding, so — per the fail-open law's missing-runtime variant — NAME the absent runtime plainly for the
# operator instead of exiting silently, and exit NON-blocking (never 2, which the platform reads as a hard
# block: the #390 fail-closed stranding). Boot carries the standing DURABLE surfacing of an unhealthy
# runtime (hooks/README §Fail-open-and-flag, D-156; the promotion is #391/#412). Fail-safe: this readout
# cannot fail the script closed.
printf '%s\n' "The engine could not run its own tools: its private Python runtime is not ready ($interp). I have not verified this step — this is not a block. If it keeps happening, the engine's environment needs to be set up (provisioning)." >&2
exit 1
