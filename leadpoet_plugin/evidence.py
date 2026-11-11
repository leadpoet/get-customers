"""One rule for every claim: a sentence may say only what its source says.

This is plain code on purpose. A model can't talk its way past a word that isn't on the page.
"""

from __future__ import annotations

import re

from .website import numbers_in

# Greek, Cyrillic, invisible and full-width characters can make a banned word look like a harmless one.
_LOOKALIKE = re.compile("[Ͱ-ϿЀ-ԯ\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff＀-￯]")
NEGATION = re.compile(r"\b(?:not|no|never|without|cannot|none|nor|neither)\b|n['’]t\b", re.IGNORECASE)
# Words a phrase can't end on. "Time spent on" stops mid-phrase. "Manual work" doesn't.
GRAMMAR = frozenset(
    "a an the and or but so to of in on at for from with by as into per via is are was were be been being am do does did "
    "it its this that these those which who whom whose what when where how why i me my we us our you your yours they "
    "them their he him his she her has have had having can could will would may might should also just then than there "
    "here if up out over about not no never without cannot none nor neither don't doesn't isn't aren't won't can't "
    "each both any some".split())
# Grammar and plain verbs. A word that makes a promise (all, best, faster, free, only) must be in the source.
GLUE = GRAMMAR | frozenset(
    "help helps let lets give gives offer offers include includes provide provides use uses using get gets keep keeps "
    "work works".split())


def has_lookalike(text: str) -> bool:
    return bool(_LOOKALIKE.search(text))


def words(text: str) -> list[str]:
    """Hyphens split, so "cold-chain" and "cold chain" count the same."""
    return [re.sub(r"'s$", "", word).strip("'") for word in re.findall(r"[a-z0-9']+", text.lower().replace("’", "'"))]


def stem(word: str) -> str:
    """Close enough to match "ships" and "shipping", strict enough to keep "stopped" and "started" apart."""
    for ending in ("ing", "ed", "es", "s", "ly"):
        if word.endswith(ending) and len(word) - len(ending) >= 3:
            word = word[:-len(ending)]
            if ending in ("ing", "ed") and len(word) > 3 and word[-1] == word[-2]:
                word = word[:-1]
            break
    if len(word) > 3 and word[-1] in "ey":
        word = word[:-1] + ("i" if word[-1] == "y" else "")
    return word[:6]


def _without_numbered_names(text: str, names: str) -> str:
    """E2B and Mem0 are names. The 2 and the 0 in them are not numbers that need a source."""
    for name in names.split():
        if any(character.isdigit() for character in name):
            text = re.sub(rf"(?<!\w){re.escape(name)}(?!\w)", " ", text, flags=re.IGNORECASE)
    return text


def unsupported(text: str, source: str, names: str = "", allowed: frozenset[str] = frozenset(), ordered: bool = True,
                negation: bool = True) -> str:
    """What the source doesn't back up, or "" when all of it is backed up.

    Every word must be in the source, apart from grammar and the names passed in. In order too, so
    "ships to France, not Germany" can't become "ships to Germany, not France". Turn negation off only when text can't be matched to one sentence of the
    source, and refuse negative wording yourself.
    """
    if has_lookalike(text):
        return "an unusual character"
    if not numbers_in(_without_numbered_names(text, names)) <= numbers_in(source):
        return "a number"
    if negation and bool(NEGATION.search(text)) != bool(NEGATION.search(source)):
        return "the word no or not"
    stems = [stem(word) for word in words(source)]
    free = {stem(word) for word in words(names)} | {stem(word) for word in allowed}
    position = checked = 0
    for word in words(text):
        if word.isdigit():
            checked += 1
            continue
        if word in GLUE or word in allowed:
            continue
        current = stem(word)
        if current in free:
            continue
        if current not in stems:
            return word
        checked += 1
        if ordered:
            try:
                position = stems.index(current, position) + 1
            except ValueError:
                return f"{word} (out of order)"
    return "" if checked else "nothing specific"
