from __future__ import annotations

from chatbot import b2b_session as bs


def test_b2b_session_create_and_fetch():
    s1 = bs.get_or_create_session(None, "v1")
    s2 = bs.get_or_create_session(s1.session_id, "v1")
    assert s1.session_id == s2.session_id


def test_b2b_session_ttl_cleanup_via_recreate(monkeypatch):
    s1 = bs.get_or_create_session("sid-1", "v1")
    monkeypatch.setattr(bs.time, "time", lambda: s1.created_at + bs.TTL_SECONDS + 1)
    s2 = bs.get_or_create_session("sid-1", "v1")
    assert s2.created_at > s1.created_at
