from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

from .models import ActionStep, ActionType, RiskClass


class PolicyViolation(RuntimeError):
    """The requested action is outside the configured safety policy."""


class ConfirmationRequired(RuntimeError):
    """A risky action requires an explicit approval token."""


@dataclass(frozen=True)
class GuardrailPolicy:
    allowed_origins: tuple[str, ...]
    allowed_route_prefixes: tuple[str, ...] = ()
    allowed_actions: frozenset[ActionType] = field(
        default_factory=lambda: frozenset(ActionType)
    )
    risky_actions: frozenset[ActionType] = field(default_factory=frozenset)
    require_confirmation_for_risky: bool = True

    def check_url(self, url: str) -> None:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self.allowed_origins:
            raise PolicyViolation(f"origin is not allowlisted: {origin}")
        if self.allowed_route_prefixes and not any(
            parsed.path.startswith(prefix) for prefix in self.allowed_route_prefixes
        ):
            raise PolicyViolation(f"route is not allowlisted: {parsed.path}")

    def check_step(self, step: ActionStep, *, confirmed: bool = False) -> None:
        if step.action not in self.allowed_actions:
            raise PolicyViolation(f"action is not allowlisted: {step.action.value}")
        if (
            self.require_confirmation_for_risky
            and (step.risk is RiskClass.RISKY or step.action in self.risky_actions)
            and not confirmed
        ):
            raise ConfirmationRequired(f"confirmation required for step {step.id}")

    @classmethod
    def local_demo(cls, origin: str) -> "GuardrailPolicy":
        return cls(
            allowed_origins=(origin,),
            allowed_route_prefixes=("/",),
            risky_actions=frozenset(),
        )
