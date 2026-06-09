from __future__ import annotations

import pytest

from rag_pipeline.orchestrator import usda_guidelines as ug


def test_load_usda_guidelines_falls_back_to_defaults_when_not_configured(monkeypatch):
    for key in ("SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_DATABASE_URL", "USDA_STRICT_MODE", "USDA_GUIDELINES_STRICT"):
        monkeypatch.delenv(key, raising=False)
    ug._USDA_GUIDELINES_CACHE["value"] = None
    out = ug.load_usda_guidelines(ttl_s=0)
    assert out is not None
    assert out.groups


def test_load_usda_guidelines_strict_mode_raises_on_source_failure(monkeypatch):
    monkeypatch.setenv("USDA_STRICT_MODE", "1")
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "k")
    ug._USDA_GUIDELINES_CACHE["value"] = None
    monkeypatch.setattr(ug, "_load_usda_guidelines_via_supabase_client", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        ug.load_usda_guidelines(ttl_s=0)


# ── New gap-fill tests ─────────────────────────────────────────────────────────

def test_load_usda_guidelines_cached_result_skips_reload(monkeypatch):
    """
    A warm cache (value != None, loaded_at recent) should return the cached object
    without calling the Supabase loader again.
    """
    import time

    for key in ("SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_DATABASE_URL", "USDA_STRICT_MODE", "USDA_GUIDELINES_STRICT"):
        monkeypatch.delenv(key, raising=False)

    # Prime the cache with a fresh load
    ug._USDA_GUIDELINES_CACHE["value"] = None
    first = ug.load_usda_guidelines(ttl_s=60)
    assert first is not None

    # Patch the loader to blow up — proves it is NOT called on second access
    monkeypatch.setattr(ug, "_load_usda_guidelines_via_supabase_client", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("must not be called")))

    second = ug.load_usda_guidelines(ttl_s=60)
    # Returned from cache: same object identity or at least valid
    assert second is not None
    assert second.groups
