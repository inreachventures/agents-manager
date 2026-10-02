"""Keys, tickets, names and branch names."""

from __future__ import annotations

import re
import unicodedata

TICKET_RE = re.compile(r"^[A-Z][A-Z0-9]*-\d+$")
MAX_NAME_WORDS = 3


class NamingError(ValueError):
    pass


def normalize_ticket(ticket: str) -> str:
    t = ticket.strip().upper()
    if not TICKET_RE.match(t):
        raise NamingError(f"invalid ticket id {ticket!r}: expected something like PROJ-313")
    return t


def auto_key(n: int) -> str:
    return f"WS-{n}"


def normalize_name(name: str) -> str:
    """'Dark Mode Toggle!' -> 'dark mode toggle' (at most 3 words)."""
    ascii_ = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    words = re.findall(r"[a-z0-9]+", ascii_.lower())
    if not words:
        raise NamingError(f"invalid name {name!r}: needs at least one word")
    if len(words) > MAX_NAME_WORDS:
        raise NamingError(f"name {name!r} has {len(words)} words; use at most {MAX_NAME_WORDS}")
    return " ".join(words)


def slug(name: str) -> str:
    return normalize_name(name).replace(" ", "-")


def branch_name(key: str, ticket: str | None, name: str) -> str:
    """proj-313-dark-mode-toggle, or ws-7-dark-mode-toggle without a ticket."""
    return f"{(ticket or key).lower()}-{slug(name)}"


def branch_carries_id(branch: str, key: str, ticket: str | None) -> bool:
    b = branch.lower()
    return any(ident.lower() in b for ident in (ticket, key) if ident)


def symlink_name(key: str, ticket: str | None, name: str | None) -> str | None:
    """Convenience symlink name, e.g. PROJ-313-dark-mode-toggle. None if nothing to add to the key."""
    if not name and not ticket:
        return None
    parts = [ticket or key]
    if name:
        parts.append(slug(name))
    link = "-".join(parts)
    return None if link == key else link
