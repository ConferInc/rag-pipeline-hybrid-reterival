from __future__ import annotations

import pytest

from api import app as app_mod


def test_profile_resolution_prefers_member_role_over_scope(monkeypatch):
    captured = {}

    def _fake_resolve(*_a, **kwargs):
        captured.update(kwargs)
        return {"diets": [], "allergens": [], "health_conditions": []}

    monkeypatch.setattr(app_mod, "resolve_profile_for_recommendation", _fake_resolve)
    monkeypatch.setattr(app_mod, "_infer_default_scope", lambda *_a, **_k: "couple")

    app_mod._resolve_profile(
        driver=object(),
        customer_id="cust-1",
        database=None,
        household_id="hh-1",
        scope="couple",
        target_member_role=None,
    )
    assert captured["target_member_role"] == "primary_adult"


def test_is_aggregated_profile_flags_family_or_role():
    assert app_mod._is_aggregated_profile(scope="family") is True
    assert app_mod._is_aggregated_profile(target_member_role="child") is True
    assert app_mod._is_aggregated_profile(scope="individual") is False


@pytest.mark.asyncio
async def test_request_body_size_middleware_returns_413_for_large_body():
    class Req:
        method = "POST"
        headers = {"content-length": str((app_mod._MAX_BODY_KB * 1024) + 1)}

    async def _next(_req):
        raise AssertionError("call_next should not be reached")

    resp = await app_mod._request_body_size_middleware(Req(), _next)
    assert resp.status_code == 413
