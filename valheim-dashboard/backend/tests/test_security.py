"""Tests for sessions, CSRF, and exact Host/Origin/Content-Type checks."""

import unittest

from src.security import (
    RequestRejected,
    SessionStore,
    parse_session_cookie,
    require_safe_state_change,
    session_cookie_header,
)


class SessionStoreTests(unittest.TestCase):
    def test_create_returns_unique_tokens(self) -> None:
        store = SessionStore()
        first = store.create()
        second = store.create()
        self.assertNotEqual(first.session_id, second.session_id)
        self.assertNotEqual(first.csrf_token, second.csrf_token)

    def test_get_returns_none_for_unknown_session(self) -> None:
        store = SessionStore()
        self.assertIsNone(store.get("unknown"))
        self.assertIsNone(store.get(None))

    def test_get_returns_the_created_session(self) -> None:
        store = SessionStore()
        session = store.create()
        self.assertEqual(store.get(session.session_id), session)

    def test_store_is_bounded(self) -> None:
        store = SessionStore(max_sessions=4, ttl_seconds=3600)
        sessions = [store.create() for _ in range(10)]
        self.assertLess(len(store._sessions), 10)
        self.assertIsNone(store.get(sessions[0].session_id))
        self.assertIsNotNone(store.get(sessions[-1].session_id))

    def test_expired_sessions_are_rejected(self) -> None:
        store = SessionStore(ttl_seconds=-1)
        session = store.create()
        self.assertIsNone(store.get(session.session_id))


class CookieTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        header = session_cookie_header("abc123")
        self.assertIn("abc123", header)
        cookie_pair = header.split(";", 1)[0]
        self.assertEqual(parse_session_cookie(cookie_pair), "abc123")

    def test_missing_cookie_header_returns_none(self) -> None:
        self.assertIsNone(parse_session_cookie(None))
        self.assertIsNone(parse_session_cookie(""))

    def test_malformed_cookie_header_returns_none(self) -> None:
        self.assertIsNone(parse_session_cookie("\x00bad"))


class RequireSafeStateChangeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = SessionStore()
        self.session = self.store.create()
        self.allowed_host = "127.0.0.1:8080"

    def _call(self, **overrides):
        arguments = {
            "method": "POST",
            "host_header": self.allowed_host,
            "origin_header": f"http://{self.allowed_host}",
            "content_type_header": "application/json",
            "csrf_header": self.session.csrf_token,
            "session": self.session,
            "allowed_host": self.allowed_host,
        }
        arguments.update(overrides)
        return require_safe_state_change(**arguments)

    def test_valid_request_passes(self) -> None:
        self._call()

    def test_rejects_non_post_method(self) -> None:
        with self.assertRaises(RequestRejected) as context:
            self._call(method="GET")
        self.assertEqual(context.exception.code, "method_not_allowed")

    def test_rejects_mismatched_host(self) -> None:
        with self.assertRaises(RequestRejected) as context:
            self._call(host_header="evil.example:8080")
        self.assertEqual(context.exception.code, "host_rejected")

    def test_rejects_mismatched_origin(self) -> None:
        with self.assertRaises(RequestRejected) as context:
            self._call(origin_header="http://evil.example")
        self.assertEqual(context.exception.code, "origin_rejected")

    def test_rejects_missing_origin(self) -> None:
        with self.assertRaises(RequestRejected) as context:
            self._call(origin_header=None)
        self.assertEqual(context.exception.code, "origin_rejected")

    def test_rejects_non_json_content_type(self) -> None:
        with self.assertRaises(RequestRejected) as context:
            self._call(content_type_header="text/plain")
        self.assertEqual(context.exception.code, "content_type_rejected")

    def test_rejects_missing_session(self) -> None:
        with self.assertRaises(RequestRejected) as context:
            self._call(session=None)
        self.assertEqual(context.exception.code, "session_required")

    def test_rejects_missing_csrf_token(self) -> None:
        with self.assertRaises(RequestRejected) as context:
            self._call(csrf_header=None)
        self.assertEqual(context.exception.code, "csrf_rejected")

    def test_rejects_wrong_csrf_token(self) -> None:
        with self.assertRaises(RequestRejected) as context:
            self._call(csrf_header="wrong-token")
        self.assertEqual(context.exception.code, "csrf_rejected")


if __name__ == "__main__":
    unittest.main()
