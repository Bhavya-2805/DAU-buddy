"""Tests for chat question logging (#70)."""

import logging
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import question_log
from core.question_log import caller_role_bucket, log_chat_question, redact_personal_details

ANSWER = "Test fallback answer"


class _FakeDb:
    """Fake db_connection() that records execute() calls."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.executed: list[tuple[str, tuple]] = []

    def __call__(self):
        db = self

        class _Conn:
            def __enter__(self_inner):
                if db.fail:
                    raise RuntimeError("simulated database failure")
                conn = MagicMock()
                cursor = MagicMock()
                cursor.execute.side_effect = lambda sql, params=None: db.executed.append((sql, params))
                conn.cursor.return_value.__enter__.return_value = cursor
                conn.cursor.return_value.__exit__.return_value = False
                return conn

            def __exit__(self_inner, *exc):
                return False

        return _Conn()


@pytest.mark.parametrize("role_in, role_out", [
    ("Student", "student"),
    ("student", "student"),
    ("Student / Maintainer", "student"),
    ("Faculty", "faculty"),
    ("Staff", "staff"),
    ("Maintainer", "student"),
    ("Unknown", "student"),
    ("", "student"),
    (None, "student"),
])
def test_caller_role_bucket(role_in, role_out):
    assert caller_role_bucket(role_in) == role_out


class TestRowContents:
    def test_writes_exactly_one_row_without_identity(self):
        db = _FakeDb()
        with patch.object(question_log, "db_connection", db):
            assert log_chat_question("Where is Prof Sunitha's office?", "Faculty") is True

        assert len(db.executed) == 1
        sql, params = db.executed[0]
        assert "INSERT INTO chat_questions" in sql
        assert params == ("Where is Prof Sunitha's office?", "faculty")
        column_list = sql.split("VALUES")[0].lower()
        for forbidden in ("email", "ip", "history", "user"):
            assert forbidden not in column_list.replace("caller_role", "")

    def test_time_is_cut_to_the_day(self):
        db = _FakeDb()
        with patch.object(question_log, "db_connection", db):
            log_chat_question("hello", "Student")
        assert "date_trunc('day'" in db.executed[0][0]

    def test_blank_question_is_not_stored(self):
        db = _FakeDb()
        with patch.object(question_log, "db_connection", db):
            assert log_chat_question("   ", "Student") is False
        assert db.executed == []


@pytest.mark.parametrize("text, expected", [
    ("mail me at 202001001@daiict.ac.in", "mail me at [email]"),
    ("my id is 202001001", "my id is [number]"),
    ("call +919876543210 now", "call [number] now"),
    ("call 98765 43210 now", "call [number] now"),
    # kept as is
    ("classes on 2026-08-07 at 10:00", "classes on 2026-08-07 at 10:00"),
    ("room 1203 and CS 101", "room 1203 and CS 101"),
])
def test_redaction(text, expected):
    assert redact_personal_details(text) == expected


def test_redacted_text_is_what_gets_stored():
    db = _FakeDb()
    with patch.object(question_log, "db_connection", db):
        log_chat_question("my id is 202001001, mail a@b.com", "Student")
    assert db.executed[0][1][0] == "my id is [number], mail [email]"


def test_insert_failure_is_logged_and_does_not_raise(caplog):
    with patch.object(question_log, "db_connection", _FakeDb(fail=True)):
        with caplog.at_level(logging.ERROR):
            result = log_chat_question("anything", "Student")
    assert result is False
    assert any("Failed to log chat question" in r.getMessage() for r in caplog.records)


class TestChatEndpoint:

    @pytest.fixture()
    def client(self):
        from api.routes import chat as chat_module

        patches = [
            patch.object(chat_module, "is_gemini_available", return_value=False),
            patch.object(chat_module, "openai_configured", return_value=False),
            patch.object(chat_module, "is_openai_available", return_value=False),
            patch.object(chat_module, "process_fallback_message", new=MagicMock(return_value=ANSWER)),
            patch.object(chat_module, "db_connection", _FakeDb()),
        ]
        for p in patches:
            p.start()

        app = FastAPI()
        app.include_router(chat_module.router)
        app.dependency_overrides[chat_module.authenticate_request] = (
            lambda: ("202001001@daiict.ac.in", "Student")
        )
        yield TestClient(app, raise_server_exceptions=False)
        for p in patches:
            p.stop()

    def test_one_question_logs_one_row_with_no_email(self, client):
        db = _FakeDb()
        with patch.object(question_log, "db_connection", db):
            resp = client.post("/chat", json={"message": "When is the next holiday?", "history": []})

        assert resp.status_code == 200
        assert resp.json()["response"] == ANSWER
        assert len(db.executed) == 1
        sql, params = db.executed[0]
        assert "chat_questions" in sql
        assert params == ("When is the next holiday?", "student")
        assert "202001001" not in repr(db.executed)     

    def test_chat_still_answers_when_the_insert_raises(self, client, caplog):
        with patch.object(question_log, "db_connection", _FakeDb(fail=True)):
            with caplog.at_level(logging.ERROR):
                resp = client.post("/chat", json={"message": "When is the next holiday?", "history": []})

        assert resp.status_code == 200
        assert resp.json()["response"] == ANSWER
        assert any("Failed to log chat question" in r.getMessage() for r in caplog.records)
