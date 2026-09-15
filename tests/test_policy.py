import pytest

from cua.models import ActionStep, ActionType, RiskClass
from cua.policy import ConfirmationRequired, GuardrailPolicy, PolicyViolation


def test_policy_rejects_unapproved_origin():
    policy = GuardrailPolicy.local_demo("http://127.0.0.1:8765")
    with pytest.raises(PolicyViolation):
        policy.check_url("https://example.com/account")


def test_policy_rejects_unapproved_route_on_allowlisted_origin():
    policy = GuardrailPolicy.local_demo("http://127.0.0.1:8765")
    with pytest.raises(PolicyViolation):
        policy.check_url("http://127.0.0.1:8765/admin")


def test_policy_requires_confirmation_for_risky_step():
    policy = GuardrailPolicy(
        allowed_origins=("http://127.0.0.1:8765",),
        allowed_actions=frozenset(ActionType),
        risky_actions=frozenset({ActionType.CLICK}),
    )
    step = ActionStep("submit", ActionType.CLICK, risk=RiskClass.RISKY)
    with pytest.raises(ConfirmationRequired):
        policy.check_step(step)
    policy.check_step(step, confirmed=True)
