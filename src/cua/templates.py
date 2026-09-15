from __future__ import annotations

from urllib.parse import urlparse

from .discovery import DiscoveryTemplate
from .models import BusinessOutcome, Checkpoint, CheckpointKind, ParameterSpec


def member_balance_template(target_url: str) -> DiscoveryTemplate:
    parsed = urlparse(target_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return DiscoveryTemplate(
        capability_id="member-balance-v1",
        name="Read member balance",
        description="Find a member in the synthetic member services console and read the current savings balance.",
        target={"url": target_url, "origin": origin},
        parameters={
            "member_id": ParameterSpec(
                type="string",
                description="Synthetic member identifier supplied by the caller.",
            )
        },
        output_descriptions={
            "balance": ("string", "Current savings balance displayed by the portal.", True),
        },
        checkpoint=Checkpoint(
            CheckpointKind.TEXT_PRESENT,
            "Member details",
            "The member details panel is visible.",
        ),
        business_outcomes=(
            BusinessOutcome(
                "MEMBER_NOT_FOUND",
                "The portal reported that no member matched the supplied identifier.",
                "Member not found",
            ),
        ),
    )
