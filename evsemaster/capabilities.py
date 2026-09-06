"""Per-model quirks the protocol gives no way to ask about.

Keyed off the brand and model a charger reports at login. A deny-list: an unrecognised charger
gets the permissive default, because a wrong denial hides a feature that works, which is easier
to notice than a command that quietly damages a session.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Capabilities:
    """What a charger will accept, beyond what the protocol implies."""

    # Whether a new output amperage takes effect during an active charge.
    amps_while_charging: bool = True


DEFAULT = Capabilities()

# Keys are lowercase "<brand> <model>" prefixes, matched with startswith.
# If two keys match, the longer one wins: "telestar abc123" can carve itself out of "telestar".
_QUIRKS: dict[str, Capabilities] = {
    "telestar ec311s": Capabilities(amps_while_charging=False),
}


def for_device(brand: str | None, model: str | None) -> Capabilities:
    """Capabilities of the charger identifying itself as this brand and model."""
    key = f"{brand or ''} {model or ''}".strip().lower()
    matched = [prefix for prefix in _QUIRKS if key.startswith(prefix)]
    return _QUIRKS[max(matched, key=len)] if matched else DEFAULT
