#!/usr/bin/env python3
"""`/engine-help` listing tool — the degradation-proof command index.

Backs the `/engine-help` operator command: a plain-language listing of the engine's own typed
commands so a non-engineer asking "what can I do here?" always gets an answer. It derives the listing
from committed files only — never an MCP substrate — so an outage cannot blank it (the discovery
axis; degrade-to-git-native). Two parts:

- Installed commands — the engine's OWN, engine-prefixed, operator-invocable verbs present on disk
  (`.claude/skills/engine-*/SKILL.md` and the legacy `.claude/commands/engine-*.md`), each shown as the
  command the operator types plus its one-line description. Operator-invocable = the invocation axis the
  operator can reach: `operator-typed` and `model-auto` (an omitted invocation defaulting to model-auto);
  `model-only` verbs are hidden from the operator's menu. Scoped to the engine's commands (the
  engine/operator wall, the same scope the self-election guard governs); the operator's own un-prefixed
  product commands, and the full command set, are the platform's bare `/` menu to show, not this one's.
- Available-if-installed commands — optional commands the operator could add, RELAYED from the committed
  module catalog the first-run setup maintains (a relay: the catalog's owner is provisioning; this tool
  only reads it, through the shared `module_catalog` reader so this index and the setup walkthrough cannot
  drift in how they parse it). The catalog ships empty and grows as optional modules are built, so this part
  is an empty relay today — present but with nothing to list yet.

Design fidelity notes (for a maintainer reading the source, not the operator):
- The verb shown is the TYPED name — the skill DIRECTORY (or the legacy command FILENAME), i.e. the
  string the operator actually types. The locked design says "the skill `name` (fallback: its directory)";
  on the platform the typed identity IS the directory and frontmatter `name` is only a display label
  (skill.v1 schema), so the typed name is the platform-correct source. For an engine command the two
  coincide.
- Each per-file frontmatter parse is wrapped: `validate.frontmatter` RAISES on malformed YAML (its
  halt-on-malformed posture), so a single broken command file must not crash the whole listing — the
  always-answers guarantee. This is the DELIBERATE OPPOSITE of the self-election guard
  (skill_coherence_check.engine_skills), which lets the raise propagate to fail closed: a detection guard
  must never silently pass, an operator listing must never go blank. Same discovery, opposite posture by
  design.
- The typed-name + engine-glob logic used to be a small local copy; it now lives in the shared
  skill-discovery helper (`skill_discovery`) that this listing, the self-election guard, and the Codex
  render all read — the helper the note here long predicted, which exposes the raw per-file parse and
  lets each caller keep its own posture (this listing skips a malformed file; the guard lets it raise).
"""
from __future__ import annotations
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402
import module_catalog  # noqa: E402  (the shared optional-module catalog reader — one parse path, no drift)
import skill_discovery  # noqa: E402  (the shared skill-discovery helper — one glob + parse path, no drift)

# The two runtime trees carry the SAME verbs (the Codex tree is a committed render of the Claude one), so the
# listing dedupes by typed name and surfaces a verb present in only one tree as partially installed. The trees
# and the engine- prefix (the engine/operator wall) live in skill_discovery, the shared discovery home.
# What a one-tree-only verb's listing appends, so a broken mirror is surfaced, never hidden. Every engine
# command now has both twins (no runtime-only verb is expected); the sanctioned asymmetries live in the
# provider-exception ledger, and this line simply tells the operator which runtime a verb works on today if a
# mirror ever goes missing.
_ONLY_CLAUDE_NOTE = " (currently only available when working in Claude Code)"
_ONLY_CODEX_NOTE = " (currently only available when working in Codex)"

_HEADER = "Commands you can type:"
_AVAILABLE_HEADER = "You can also add these through your Engine's setup — ask me, or run engine-setup:"
_EMPTY_AVAILABLE_LINE = "More capabilities become available as you add optional parts to your Engine through setup."
_POINTER = "New to the Engine? Ask me to open the getting-started guide — it walks you through the basics."


def installed_verbs(root: str | None = None) -> list:
    """The engine's installed operator-invocable commands as a list of {name, description}, sorted by the
    typed name. Reads only the engine-prefixed skills (through the shared `skill_discovery` helper) and keeps
    only the commands the operator can invoke — the operator-invocable axis: `operator-typed` and `model-auto`
    (an omitted invocation defaults to model-auto), but not `model-only`, which is hidden from the operator's
    menu. The discovery runs in the non-strict posture: a malformed skill file is skipped rather than allowed
    to crash the whole listing (degrade, never blank — the always-answers guarantee)."""
    # The CLAUDE source is the sole authority on operator-invocability. A model-only route renders its Codex
    # twin WITHOUT an `invocation` field (the twin's policy lives in agents/openai.yaml, not its frontmatter),
    # so reading the Codex frontmatter would default every route to model-auto and silently re-admit the whole
    # model-only surface to the operator's menu. So the hidden set is computed once from the Claude tree and
    # applied to BOTH trees below — a route the Claude source marks non-operator-invocable is hidden on every
    # runtime, matching the ADR-0336 rule that engine-help never exposes automatic routes.
    hidden = set()
    for rec in skill_discovery.records("claude", root=root, include_commands=True):
        inv = rec["frontmatter"].get("invocation") or "model-auto"
        if inv not in ("operator-typed", "model-auto"):
            hidden.add(rec["slug"])
    seen: dict = {}
    for tree in ("claude", "codex"):
        # Claude also carries the legacy flat commands; the Codex tree has none.
        for rec in skill_discovery.records(tree, root=root, include_commands=(tree == "claude")):
            name = rec["slug"]
            if name in hidden:
                continue   # non-operator-invocable on its Claude source — hidden from the menu on every runtime
            fm = rec["frontmatter"]
            inv = fm.get("invocation") or "model-auto"   # an omitted invocation is model-auto (platform default)
            if inv not in ("operator-typed", "model-auto"):
                continue   # a Codex-only oddity declaring a non-invocable value; the Claude-side set already caught model-only
            entry = seen.setdefault(name, {"name": name, "description": "", "trees": set()})
            entry["trees"].add(tree)
            if tree == "claude" or not entry["description"]:   # the Claude source's description wins
                entry["description"] = str(fm.get("description") or "") or entry["description"]
    # Annotate a one-tree-only verb ONLY when BOTH runtime trees are actually populated — a repo
    # carrying just the Claude adapter (or a minimal test tree) gets no noise; once both adapters
    # are present, a verb missing its twin is surfaced, never hidden.
    both_present = all(any(t in e["trees"] for e in seen.values()) for t in ("claude", "codex"))
    verbs = []
    for entry in seen.values():
        desc = entry["description"]
        home = None
        if both_present and entry["trees"] == {"claude"}:
            desc += _ONLY_CLAUDE_NOTE
            home = "claude"      # render with the sigil it actually answers to, whatever the ambient runtime
        elif both_present and entry["trees"] == {"codex"}:
            desc += _ONLY_CODEX_NOTE
            home = "codex"
        verbs.append({"name": entry["name"], "description": desc.strip(), "home": home})
    return sorted(verbs, key=lambda v: v["name"])


def _installed_module_ids() -> set:
    """The ids of the modules installed in this engine (the engine manifest's `packages`), or an empty set
    when it cannot be read — the available list then degrades to listing everything rather than blanking
    (degrade, never blank). The catalog lists every optional module the engine ships; one that is ALREADY
    installed is shown under the installed commands, not as something to install, so it is excluded here."""
    try:
        engine = validate.load_json(os.path.join(validate.ROOT, ".engine", "engine.json"))
        return set((engine or {}).get("packages") or {})
    except Exception:  # noqa: BLE001 — an unreadable manifest degrades to no filter, never blanks the list
        return set()


def _first_sentence(text: str) -> str:
    """A concise one-line gloss for the add-on section: the description's first sentence (up to the first
    sentence-ending '. '), or the whole thing when it is already short. Keeps the /engine-help add-on
    section a scannable index rather than reprinting the full setup-walkthrough paragraph."""
    text = (text or "").strip()
    for sep in (". ", ".\n"):
        idx = text.find(sep)
        if idx != -1:
            return text[:idx + 1]
    return text


def available_addons(catalog_path: str | None = None) -> list:
    """The optional, not-yet-installed add-ons, RELAYED from the committed module catalog — or an empty list
    when the catalog is absent, empty, or damaged (it narrows the listing, never breaks it). Returns each as
    {id, description}: the add-on's stable id and a concise one-line gloss (its description's first sentence),
    sorted by id. An already-installed module is EXCLUDED. There is no per-module `verb`: add-ons are reached
    through natural-language setup routes and the permanent engine-setup dispatcher, so this section presents
    them BY DESCRIPTION under an 'available through engine-setup' heading rather than as typeable commands.
    This tool only relays; provisioning owns the catalog and the shared `module_catalog` reader parses it, so
    this index and the first-run walkthrough cannot drift. `catalog_path` is injectable for tests."""
    installed = _installed_module_ids()
    return [{"id": e["id"], "description": _first_sentence(e["description"])}
            for e in module_catalog.entries(catalog_path) if e["id"] not in installed]


def ambient_provider() -> "str | None":
    """Which runtime the operator is typing in, for the prefix rendering: the launcher-exported
    provider tag when a hook chain set it, else the live-session marker's provider (boot records it
    at every SessionStart), else None — genuinely unknown, so the listing shows both forms."""
    try:
        import providers
        env = (os.environ.get(providers.PROVIDER_ENV) or "").strip().lower()
        if env in (providers.CLAUDE, providers.CODEX):
            return env
        record = providers.read_live_session()
        if record and record.get("provider") in (providers.CLAUDE, providers.CODEX):
            return record["provider"]
    except Exception:  # noqa: BLE001 — an unreadable seam degrades to the both-forms rendering
        pass
    return None


def _verb_line(verb: dict, prefix: "str | None" = "/") -> str:
    """One listing line. `prefix` is the typed sigil for the ambient runtime ("/" on Claude Code,
    "$" on Codex); None means the runtime is unknown, so both forms are shown once per verb. A verb
    installed for only ONE runtime always renders with THAT runtime's sigil (its note names the
    runtime), so the listing never presents a verb in a form that would not answer."""
    desc = verb.get("description") or ""
    name = verb.get("name", "")
    home = verb.get("home")
    if home:
        typed = f"/{name}" if home == "claude" else f"${name}"
    elif prefix is None:
        typed = f"/{name}  (in Codex: ${name})"
    else:
        typed = f"{prefix}{name}"
    return f"  {typed} — {desc}" if desc else f"  {typed}"


def render(installed: list, available: list, prefix: "str | None" = "/") -> str:
    """The plain-language listing the operator sees. Installed commands first (alphabetical), then the
    optional ones (alphabetical) or a single plain line when there are none — never a bare empty heading
    — and a closing pointer to the getting-started guide. A pure function of its inputs: no clock, no
    network, no MCP. `prefix` renders each verb in the ambient runtime's own typed form (None = both)."""
    lines = [_HEADER, ""]
    lines.extend(_verb_line(v, prefix) for v in installed)
    lines.append("")
    if available:
        lines.append(_AVAILABLE_HEADER)
        lines.append("")
        lines.extend(f"  {a['description']}" for a in available)
    else:
        lines.append(_EMPTY_AVAILABLE_LINE)
    lines.append("")
    lines.append(_POINTER)
    return "\n".join(lines)


def _demo() -> int:
    """An operator-runnable demonstration that the listing always answers. It prints the real listing,
    then re-runs the REAL listing logic over a throwaway temporary copy of the commands that has no
    optional-commands catalog and one deliberately broken command file — showing the listing still
    renders (the broken command skipped, the rest intact, nothing crashing). Real files are untouched."""
    import shutil
    import tempfile

    print("Your Engine's commands, the way /engine-help lists them:\n")
    live = render(installed_verbs(), available_addons())
    print(live)
    print("\n" + "-" * 70 + "\n")
    print("The same listing when a command file is broken — to show the help always answers.\n"
          "This copies your commands into a throwaway temporary folder (your real files are NOT\n"
          "touched), then plants one deliberately broken command file in the copy:\n")
    with tempfile.TemporaryDirectory() as tmp:
        dst_skills = os.path.join(tmp, ".claude", "skills")
        os.makedirs(dst_skills, exist_ok=True)
        src_skills = os.path.join(validate.ROOT, ".claude", "skills")
        for entry in sorted(glob.glob(os.path.join(src_skills, "engine-*"))):
            if os.path.isdir(entry):
                shutil.copytree(entry, os.path.join(dst_skills, os.path.basename(entry)))
        broken_dir = os.path.join(dst_skills, "engine-broken")
        os.makedirs(broken_dir, exist_ok=True)
        with open(os.path.join(broken_dir, "SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write("---\ndescription: [this line is broken\ninvocation: operator-typed\n---\n\n"
                     "## Steps\n\n1. Go.\n")
        broken_listing = render(installed_verbs(root=tmp), available_addons(None))
        print(broken_listing)
    print("\nThe broken command was skipped, the rest are still listed, and nothing crashed — so\n"
          "\"what can I do here?\" always gets an answer, even during an outage or with a damaged file.")
    # Self-check: the live listing rendered, and the throwaway-copy listing still rendered with the broken
    # command skipped — so "what can I do here?" always gets an answer, even with a damaged command file.
    ok = bool(live) and bool(broken_listing) and "engine-broken" not in broken_listing
    if not ok:
        print("\nDEMO UNEXPECTED: a listing did not render, or the broken command was not skipped.",
              file=sys.stderr)
        return 1
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    provider = ambient_provider()
    prefix = {"claude": "/", "codex": "$"}.get(provider)   # None (unknown) → both forms shown
    print(render(installed_verbs(), available_addons(), prefix))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
