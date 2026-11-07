"""Pure Authority value contracts; issuance and act queries are separate work."""

from dataclasses import dataclass
from typing import Literal

from .guardrail import EFFECT_CLASSES


@dataclass(frozen=True)
class Answer:
    outcome: Literal["allow", "deny", "held"]
    code: str | None
    reason: str | None
    unblocker: str | None
    slip_id: str | None
    revision: int | None

    def __post_init__(self):
        if self.outcome not in ("allow", "deny", "held"):
            raise ValueError("unknown answer outcome")
        if self.outcome == "allow":
            if self.code is not None:
                raise ValueError("allow has no answer code")
        elif not all(isinstance(value, str) and value
                     for value in (self.code, self.reason, self.unblocker)):
            raise ValueError("deny and held require code, reason and unblocker")
        if (self.slip_id is None) != (self.revision is None):
            raise ValueError("slip id and revision must travel together")
        if self.slip_id is not None:
            if not isinstance(self.slip_id, str) or not self.slip_id:
                raise ValueError("slip id must be a nonempty string")
            if type(self.revision) is not int:
                raise ValueError("slip revision must be an integer")


@dataclass(frozen=True)
class Effect:
    kind: str
    repository_id: str | None
    ref: str | None = None
    pr: int | None = None
    head: str | None = None
    path: str | None = None

    def __post_init__(self):
        if self.kind not in EFFECT_CLASSES:
            raise ValueError("unknown effect kind")
