"""refusals.py — the one base type for a refusal written to be read by the operator, and the recovery
sentences every such refusal shares.

WHY ONE TYPE. The memory server's boundary used to translate refusals by enumerating classes, and the class it
named for write refusals (`MutationAuthorityError`) also carries invariant failures and tamper signals — some
with absolute paths and raw writer identifiers — so a seam that forwarded the class wholesale sent
polished-looking sentences to the model for what were engine bugs (StarshipSuperjam/engine-template#1196).
`EngineRefusal` is raised ONLY where a sentence was written for the operator: plain words, no path, no
fingerprint, no commit, no writer identifier, no spliced exception text. The seam translates by one
`isinstance` on it and nothing else; every other exception stays the masked crash it is.

WHY THE SENTENCES LIVE HERE. Reads (the memory server's outcome notes) and writes (`_stale_refusal`) tell the
operator the same two things when a restart is what helps — the concrete action, and where to go if it keeps
happening — and they used to say them in different words (StarshipSuperjam/engine-template#1211). One home,
imported by both.
"""


class EngineRefusal(Exception):
    """A refusal whose text is written for the operator. Subclass beside the module's own exception type
    (`class PinRefused(EngineRefusal, ValueError)`), so existing handlers keep catching what they caught."""


RESTART_ACTION = ("To fully reconnect, quit Claude Desktop completely and reopen it so the memory server restarts "
                  "(in a Codex session, end the session and start a new one).")
ESCALATION = "If this keeps happening after a restart, run /engine-status and open an engine issue."
