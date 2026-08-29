#!/usr/bin/env python3
"""Cycle-free root-tool adapter for the memory mutation authority.

Most Engine tools can install the shared guard directly. A small number of dependency leaves must remain
importable without importing the memory package; they use this adapter, which resolves the same guard only when
the registered writer is actually called. The registry id remains visible for static coverage checks.
"""
from __future__ import annotations

import functools


def _lazy_guard(entry_id: str, function):
    resolved = []

    @functools.wraps(function)
    def guarded(*args, **kwargs):
        if not resolved:
            from memory import mutation_authority
            resolved.append(mutation_authority._guard(entry_id, function))
        return resolved[0](*args, **kwargs)

    guarded.__engine_registry_id__ = entry_id
    return guarded


def install(namespace: dict, registrations: dict[str, str]) -> tuple[str, ...]:
    """Install lazy shared-authority guards for an explicit, reviewable root-tool writer set."""
    installed = []
    for function_name, entry_id in registrations.items():
        function = namespace.get(function_name)
        if not callable(function):
            raise RuntimeError(f"registered persistent writer {function_name} is unavailable")
        namespace[function_name] = _lazy_guard(entry_id, function)
        installed.append(entry_id)
    return tuple(installed)
