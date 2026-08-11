"""Prompt text, kept as files rather than string literals.

Almost all of this module's git history is prompt work, and a prompt embedded
in a 4,000-line Python file cannot be diffed without the diff being buried in
code, cannot be reviewed by anyone who does not read Python, and cannot be
swapped for an A/B variant without an edit to the module that uses it.

The loader returns a plain `str`, deliberately. `analyze.CUT_PROMPT` is a
module-level name that tests reassign to check cache invalidation, and
`prompt_version` hashes whatever it is handed. A lazy or wrapper object would
break both, so the files are read once at import and become ordinary strings.

`prompt_version` hashes the *content*, so moving a prompt from a literal to a
file is invisible to the cache only if the bytes are identical. They are: the
files were written out of the loaded literals and verified to round-trip to the
same hash (`cut.md` is `d6416d2b`, which is live in cached cut keys). Editing a
file here changes that hash and re-asks the affected cuts once, which is the
intended behaviour — see `cut_key`.
"""

from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent


def load(name: str) -> str:
    """Read a prompt file verbatim.

    No `.strip()`, no normalisation: trailing whitespace is part of the content
    hash, and `session.md` genuinely ends with a trailing space that was in the
    literal. Stripping it here would silently invalidate every cached cut.
    """
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


SYSTEM_PROMPT = load("session.md")
CUT_PROMPT = load("cut.md")
CHAT_PROMPT = load("chat.md")
