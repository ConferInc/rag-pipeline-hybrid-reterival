from __future__ import annotations

from datetime import datetime, timedelta, timezone

from chatbot import session as s


def test_session_create_and_get_same_object():
    one = s.get_or_create_session("cust1")
    two = s.get_or_create_session("cust1", one.session_id)
    assert one.session_id == two.session_id


def test_expired_session_auto_cleaned():
    sess = s.get_or_create_session("cust2")
    sess.last_activity = datetime.now(timezone.utc) - timedelta(minutes=31)
    s.cleanup_expired()
    assert sess.session_id not in s._sessions
