"""
Household-aware profile resolution for recommendations.

Provides fetch_household_profile, aggregate_profile, and resolve_profile_for_recommendation.
These are additive — the existing pipeline is unchanged; handlers can call these when
family_scope or member_id context is available.
"""

from rag_pipeline.profile.household_profile import (
    aggregate_profile,
    fetch_household_profile,
    get_household_id_for_customer,
    get_household_type,
    resolve_profile_for_recommendation,
    resolve_profile_for_role,
)

__all__ = [
    "aggregate_profile",
    "fetch_household_profile",
    "get_household_id_for_customer",
    "get_household_type",
    "resolve_profile_for_recommendation",
    "resolve_profile_for_role",
]
