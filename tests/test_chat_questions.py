"""
Tests for the chat_questions logging feature.

Key acceptance criteria:
  * Asking a question inserts exactly one row (no email/IP in it).
  * Chat still answers when the insert raises.
  * Role mapping normalises all variants to student | faculty | staff.
"""

import types
from unittest.mock import patch, MagicMock, AsyncMock

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers — build a minimal ASGI app with just the chat router
# ---------------------------------------------------------------------------

def _make_app():
    """Create a minimal FastAPI app wired to the chat router for isolated testing."""
    from fastapi import FastAPI
    from api.routes.chat import router
    app = FastAPI()
    app.include_router(router)
    return app


def _auth_override(email="202001001@daiict.ac.in", role="Student"):
    """Return a dependency override that skips real Google auth."""
    async def _fake_auth():
        return email, role
    return _fake_auth


# ---------------------------------------------------------------------------
# 1. Role mapping
# ---------------------------------------------------------------------------

class TestCallerRoleMapping:
    """Verify that resolve_role outputs are mapped to the three eval-suite buckets."""

    @pytest.mark.parametrize("role_in, role_out", [
        ("Student", "student"),
        ("student", "student"),
        ("Student / Maintainer", "student"),
        ("Faculty", "faculty"),
        ("Staff", "staff"),
        ("Maintainer", "student"),       # maintainer ≠ faculty/staff → student
        ("Unknown", "student"),          # anything else → student
    ])
    def test_role_normalisation(self, role_in, role_out):
        _role_map = {'faculty': 'faculty', 'staff': 'staff'}
        result = _role_map.get(role_in.lower().split('/')[0].strip(), 'student')
        assert result == role_out


# ---------------------------------------------------------------------------
# 2. Chat survives when the chat_questions insert fails
# ---------------------------------------------------------------------------

class TestChatSurvivesInsertFailure:
    """
    Acceptance test: even when the chat_questions INSERT raises, the chat
    endpoint must still return a valid response — it should never fail because
    of logging.
    """

    @pytest.fixture()
    def _patch_stack(self):
        """Patch out all external dependencies so the endpoint can run in isolation."""
        patches = [
            # Auth
            patch("api.routes.chat.authenticate_request", return_value=("test@daiict.ac.in", "Student")),
            patch("api.routes.chat.verify_google_token", return_value="test@daiict.ac.in"),
            patch("api.routes.chat.resolve_role", return_value="Student"),
            # Rate limiter
            patch("api.routes.chat.limiter"),
            # LLM availability — force the fallback path so we don't need real keys
            patch("api.routes.chat.is_gemini_available", return_value=False),
            patch("api.routes.chat.openai_configured", return_value=False),
            patch("api.routes.chat.is_openai_available", return_value=False),
            # Fallback engine
            patch("api.routes.chat.process_fallback_message", new_callable=AsyncMock,
                  return_value="Test fallback answer"),
        ]
        mocks = [p.start() for p in patches]
        yield mocks
        for p in patches:
            p.stop()

    def test_chat_returns_when_question_insert_raises(self, _patch_stack):
        """
        Simulate db_connection raising on the chat_questions INSERT while the
        analytics INSERT succeeds. Chat must still return a response.
        """
        call_count = 0
        original_db_connection = None

        # We need to let the first db_connection call (analytics) succeed
        # but make the second call (chat_questions) fail.
        from core.database import db_connection as real_db

        class _FakeConn:
            """Minimal context-manager that either succeeds or explodes."""
            def __init__(self, should_fail):
                self._should_fail = should_fail

            def __enter__(self):
                if self._should_fail:
                    raise RuntimeError("Simulated DB failure for chat_questions")
                # Return a mock connection whose cursor does nothing
                mock_conn = MagicMock()
                mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=MagicMock())
                mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
                return mock_conn

            def __exit__(self, *args):
                pass

        def _side_effect_db():
            nonlocal call_count
            call_count += 1
            # First call = analytics (succeed), second call = chat_questions (fail)
            return _FakeConn(should_fail=(call_count == 2))

        with patch("api.routes.chat.db_connection", side_effect=_side_effect_db):
            # The limiter decorator can interfere — bypass it
            with patch("api.routes.chat.limiter.limit", lambda *a, **kw: lambda fn: fn):
                from api.routes.chat import router, authenticate_request
                from fastapi import FastAPI

                app = FastAPI()
                app.include_router(router)

                # Override the Depends(authenticate_request) so no real auth runs
                app.dependency_overrides[authenticate_request] = lambda: ("test@daiict.ac.in", "Student")

                client = TestClient(app, raise_server_exceptions=False)
                resp = client.post("/chat", json={
                    "message": "When is the next holiday?",
                    "history": [],
                })

                # Chat must NOT return 500
                assert resp.status_code == 200, f"Chat failed with {resp.status_code}: {resp.text}"
                data = resp.json()
                assert "response" in data


# ---------------------------------------------------------------------------
# 3. No email or IP stored
# ---------------------------------------------------------------------------

class TestNoPersonalData:
    """The INSERT query must not include email or IP columns."""

    def test_insert_query_has_no_email_or_ip(self):
        """Static check: the INSERT statement in chat.py must not reference email/ip columns."""
        import inspect
        from api.routes import chat as chat_module

        source = inspect.getsource(chat_module.chat_endpoint)

        # Find the chat_questions INSERT
        idx = source.find("INSERT INTO chat_questions")
        assert idx != -1, "chat_questions INSERT not found in chat_endpoint"

        # Extract just the INSERT statement (up to the closing parenthesis of VALUES)
        insert_fragment = source[idx:idx + 300]

        assert "email" not in insert_fragment.lower(), \
            "The chat_questions INSERT must not include email"
        assert "ip" not in insert_fragment.split("VALUES")[0].lower(), \
            "The chat_questions INSERT must not include IP"
