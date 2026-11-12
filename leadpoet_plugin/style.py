"""How a reply looks. The words are written elsewhere, and this only adds emphasis for the surface showing them.

Chat apps get light Markdown, which Hermes turns into each app's own bold and monospace, so a
command can be told from the sentence around it. A terminal gets the same emphasis as colour.
Every other surface gets the text exactly as written.

Nothing here changes what is approved or sent. Codes are fingerprints of the stored words, never of this.
"""

from __future__ import annotations

import os
import re
import sys

# Surfaces whose Hermes adapter turns Markdown into native formatting, or strips it cleanly.
MARKDOWN_PLATFORMS = {"signal", "telegram", "whatsapp", "whatsapp_cloud", "discord", "slack", "matrix", "mattermost",
                      "google_chat", "bluebubbles", "sms", "irc", "line", "feishu", "qqbot", "weixin", "photon"}
MARKDOWN = ("**", "**", "`", "`")
ANSI = ("\033[1m", "\033[22m", "\033[36m", "\033[39m")

_ACTIONS = ("go|open|write|approve|use|all|skip|list|check|voice|about|buyer|reset|help|"
            "send|edit|redraft|status")
_ARGUMENT = r"(?:[0-9a-f]{4}\b|<[^>\n]+>|confirm\b|[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/[^\s`]*)?)"
_START = r"(?<![^\s(])"  # A command starts a line or follows a space, so the same letters inside a web address aren't one.
# A search is opened by its name, which may be several words, so that command runs to the end of its line.
_COMMAND = re.compile(rf"{_START}/customers open (?!<)[^\n`*]*[^\n`*\s]$|"
                      rf"{_START}/(?:customers|desk)\b(?: (?:{_ACTIONS})\b)?(?: {_ARGUMENT})*")
_SHELL = re.compile(rf"{_START}hermes (?:config set LEADPOET_API_KEY <[^>\n]+>|gateway restart|plugins [a-z]+ leadpoet)")

_EMAIL = re.compile(r"^Email \d+ \([^)\n]{1,60}\):")
_LABEL = re.compile(r"^(Who buys|Why they buy|Roles|Size|Where|Why them|Personal line|Source|Your reply|"
                    r"Buying signals Leadpoet looks for|Proof from (?:your own description|\S{1,80})|Email \d+ \([^)\n]{1,60}\)):")
# What follows these is text that will be sent as it stands, so it's shown as it stands, up to one of these.
# Whole lines only, so a body line that merely begins the same way stays part of the body.
_EMAIL_END = re.compile(r"^(?:---|Leadpoet adds your booking link, if you've set one, .*|"
                        r"Approve (?:these exact emails|all \d+ exactly as shown): /customers .*)$")
_REPLY_END = re.compile(r"^(?:/desk (?:send|edit|redraft|skip)\b.*|Fill in the \[ADD: \.\.\.\] part first\..*)$")
_BULLET = re.compile(r"^• ([^:\n]{2,40}):(?= )")
# With several searches going, news starts with the name of the one it's about.
_LEAD = re.compile(r"^((?:[^\n:]{1,80}: )?\d+ verified contacts? (?:is|are) in\.|Approved [^\n]{1,80}\.$|Sent to [^\n]{1,120}\.$|"
                   r"Live\.|Your key is in\.|[^\n]{1,120} replied(?= \())")
_DESK_HEAD = re.compile(r"^(\d+ of \d+ · )([^·\n]+)( · .*)$")
_PERSON = re.compile(r"^[^\n:]{2,80}(?:, [^\n:]{2,80})? at [^\n:]{2,80}$")  # A contact may have no role.
_ADDRESS = re.compile(r"^\S+@\S+\.\S+$")


def _wants_colour() -> bool:
    if os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return False
    try:
        return bool(sys.stdout.isatty())
    except Exception:
        return False


def mark(text: str, marks: tuple[str, str, str, str] = MARKDOWN) -> str:
    """Add emphasis to names, labels and commands. A line someone else wrote (a quoted reply) is left alone."""
    bold_on, bold_off, code_on, code_off = marks

    def bold(value: str) -> str:
        # A name that carries its own marks, or ends in a space, would break the pair around it.
        return value if "*" in value or "`" in value or value != value.strip() else f"{bold_on}{value}{bold_off}"

    def code(match: re.Match) -> str:
        return f"{code_on}{match.group(0)}{code_off}"

    lines = text.split("\n")
    out: list[str] = []
    ends: re.Pattern | None = None
    proof = False  # Under "Proof from", a bullet is a sentence from their site. A colon in it is theirs, not a label.
    for index, line in enumerate(lines):
        proof = line.startswith("Proof from ") or (proof and bool(line))
        if ends and (ends.match(line) or _EMAIL.match(line)):
            ends = None
        if ends or line.startswith(">"):
            out.append(line)
            continue
        following = lines[index + 1] if index + 1 < len(lines) else ""
        title = (index + 3 < len(lines) and line and following and not lines[index + 2]
                 and lines[index + 3].startswith("Who buys:"))  # A brief opens on the product's name.
        head = _DESK_HEAD.match(line)
        label = _LABEL.match(line) or _LEAD.match(line)
        bullet = None if proof else _BULLET.match(line)
        if title or (_PERSON.match(line) and _ADDRESS.match(following)):
            line = bold(line)
        elif head:
            line = head.group(1) + bold(head.group(2)) + head.group(3)
        elif label:
            ends = _EMAIL_END if _EMAIL.match(line) else _REPLY_END if line.startswith("Your reply:") else None
            rest = line[label.end():]  # After a label come someone's words. After news comes the next step.
            line = bold(label.group(0)) + (rest if _LABEL.match(line) else _SHELL.sub(code, _COMMAND.sub(code, rest)))
        elif bullet:
            line = "• " + bold(bullet.group(1) + ":") + _SHELL.sub(code, _COMMAND.sub(code, line[bullet.end():]))
        else:
            line = _SHELL.sub(code, _COMMAND.sub(code, line))
        out.append(line)
    return "\n".join(out)


def render(text: str, platform: str) -> str:
    """The same words, styled for where they'll be read."""
    if not isinstance(text, str) or not text:
        return text
    surface = str(getattr(platform, "value", platform) or "").lower()
    if surface == "cli":
        return mark(text, ANSI) if _wants_colour() else text
    if surface in MARKDOWN_PLATFORMS:
        return mark(text, MARKDOWN)
    return text
