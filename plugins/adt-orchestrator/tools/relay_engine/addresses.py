"""Platform address validator (DD-v3-platform § Joint lock J1-J2).

The one place that parses and refuses relay addresses and validates project,
repository and team names.  It imports only the standard library and never
raises an engine error: the engine maps each ``Reason`` onto its own code
table.

Grammar:

* project and repository names: ``[a-z0-9][a-z0-9-]{0,31}``;
* team names: ``[a-z0-9][a-z0-9-]{0,63}``;
* in-project seat address ``<team>.<role>``, cross-project
  ``<team>.<role>@<project>``; ``operator`` is the only special address.

Refusal precedence, first that applies: ``WHITESPACE_IN_TOKEN``,
``UPPERCASE``, ``AT_EMPTY_OR_REPEATED``, ``OPERATOR_QUALIFIED``,
``NOT_AN_ADDRESS``, ``BAD_CHARACTER``, ``NAME_TOO_LONG``, ``UNKNOWN_ROLE``.
``UPPERCASE`` means ASCII ``A``-``Z`` only; any other non-ASCII character is
``BAD_CHARACTER``; ``WHITESPACE_IN_TOKEN`` means any ``str.isspace()``
character.  An empty dot-separated part of the seat side is
``NOT_AN_ADDRESS``, checked before the dot count.
"""

import dataclasses
import enum
import re
import types


ROLE_WORDS = frozenset({
    "planner",
    "implementer",
    "reviewer",
    "orchestrator-planner",
    "orchestrator-reviewer",
    "master-planner",
    "master-reviewer",
    "domain-planner",
    "domain-reviewer",
})

RETIRED_ROLE_ALIASES = types.MappingProxyType({
    "pair-planner": "planner",
    "pair-implementer": "implementer",
})

SPECIAL_ADDRESSES = frozenset({"operator"})

RESERVED_SENDERS = frozenset({"system"})

_OPERATOR = "operator"
_NAME_LIMITS = {"project": 32, "repository": 32, "team": 64}
# ASCII-only character classes: any non-ASCII character fails to match.
_NAME_SHAPE = re.compile(r"[a-z0-9][a-z0-9-]*")
_ROLE_SHAPE = re.compile(r"[a-z0-9-]+")
_UPPER = re.compile(r"[A-Z]")
_LIST_TRIM = " \t"


class Reason(enum.Enum):
    """Token-level refusal reasons named by DD J2."""

    NOT_AN_ADDRESS = "not-an-address"
    AT_EMPTY_OR_REPEATED = "at-empty-or-repeated"
    UPPERCASE = "uppercase"
    BAD_CHARACTER = "bad-character"
    UNKNOWN_ROLE = "unknown-role"
    NAME_TOO_LONG = "name-too-long"
    WHITESPACE_IN_TOKEN = "whitespace-in-token"
    OPERATOR_QUALIFIED = "operator-qualified"


class AddressRefused(ValueError):
    """A token the grammar refuses, with its reason.

    ``canonical`` is set only for a retired role alias and names the
    canonical role word.
    """

    def __init__(self, reason, token, canonical=None):
        super().__init__("%s: %r" % (reason.value, token))
        self.reason = reason
        self.token = token
        self.canonical = canonical


@dataclasses.dataclass(frozen=True)
class Address:
    team: "str | None" = None
    role: "str | None" = None
    project: "str | None" = None
    special: "str | None" = None


def _character_fault(value):
    """Return the whitespace or uppercase reason for ``value``, if any."""
    if any(character.isspace() for character in value):
        return Reason.WHITESPACE_IN_TOKEN
    if _UPPER.search(value):
        return Reason.UPPERCASE
    return None


def _name_fault(value, limit):
    """Return the reason a team, project or repository name fails, if any."""
    if not _NAME_SHAPE.fullmatch(value):
        return Reason.BAD_CHARACTER
    if len(value) > limit:
        return Reason.NAME_TOO_LONG
    return None


def parse_address(token):
    """Parse one address token, or raise ``AddressRefused``."""
    def refuse(reason, canonical=None):
        raise AddressRefused(reason, token, canonical)

    fault = _character_fault(token)
    if fault:
        refuse(fault)
    seat, at, project = token.partition("@")
    if at and (not seat or not project or "@" in project):
        refuse(Reason.AT_EMPTY_OR_REPEATED)
    project = project if at else None
    if seat == _OPERATOR:
        if project is not None:
            refuse(Reason.OPERATOR_QUALIFIED)
        return Address(special=_OPERATOR)
    parts = seat.split(".")
    if len(parts) < 2 or not all(parts):
        refuse(Reason.NOT_AN_ADDRESS)
    if len(parts) > 2:
        refuse(Reason.BAD_CHARACTER)
    team, role = parts
    team_fault = _name_fault(team, _NAME_LIMITS["team"])
    project_fault = (_name_fault(project, _NAME_LIMITS["project"])
                     if project is not None else None)
    role_fault = None if _ROLE_SHAPE.fullmatch(role) else Reason.BAD_CHARACTER
    faults = {team_fault, project_fault, role_fault}
    for reason in (Reason.BAD_CHARACTER, Reason.NAME_TOO_LONG):
        if reason in faults:
            refuse(reason)
    if role not in ROLE_WORDS:
        refuse(Reason.UNKNOWN_ROLE, RETIRED_ROLE_ALIASES.get(role))
    return Address(team=team, role=role, project=project)


def parse_address_list(raw):
    """Parse a comma-separated address field.

    Only ASCII space and tab are trimmed around each element; an empty
    element, and an empty field, are refused as ``NOT_AN_ADDRESS``.  What an
    absent or empty optional field means is the caller's decision.
    """
    return [parse_address(element.strip(_LIST_TRIM))
            for element in raw.split(",")]


def render(address):
    """Return the token ``parse_address`` reads back as ``address``."""
    if address.special is not None:
        return address.special
    token = "%s.%s" % (address.team, address.role)
    if address.project is not None:
        token += "@" + address.project
    return token


def validate_name(kind, value):
    """Validate a project, repository or team name, or raise.

    An unknown ``kind`` is a caller error and raises plain ``ValueError``.
    """
    if kind not in _NAME_LIMITS:
        raise ValueError("unknown name kind %r" % (kind,))
    fault = (_character_fault(value) or
             _name_fault(value, _NAME_LIMITS[kind]))
    if fault:
        raise AddressRefused(fault, value)
