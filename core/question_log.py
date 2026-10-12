"""Anonymous logging of chat questions for the eval suite. Never stores identity."""

import re

from core import config
from core.database import db_connection

logger = config.get_logger("core.question_log")

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# 7+ digits (enrollment/phone) or "98765 43210"; dates and times are kept
_NUMBER_RE = re.compile(r"\+?\d{7,}|\b\d{5}[\s-]\d{5}\b")

_ROLE_BUCKETS = {"faculty": "faculty", "staff": "staff"}


def caller_role_bucket(role: str | None) -> str:
    """Map a resolved role to student | faculty | staff."""
    first = (role or "").lower().split("/")[0].strip()
    return _ROLE_BUCKETS.get(first, "student")


def redact_personal_details(text: str) -> str:
    return _NUMBER_RE.sub("[number]", _EMAIL_RE.sub("[email]", text))


def log_chat_question(question: str, role: str | None) -> bool:
    """Store one anonymous row. Returns False on skip or failure; never raises."""
    try:
        cleaned = redact_personal_details(question).strip()
        if not cleaned:
            return False
        with db_connection() as conn:
            with conn.cursor() as cursor:
                # day only, so the row can't be matched by time to the analytics row
                cursor.execute(
                    "INSERT INTO chat_questions (question, caller_role, created_at) "
                    "VALUES (%s, %s, date_trunc('day', CURRENT_TIMESTAMP))",
                    (cleaned, caller_role_bucket(role)),
                )
        return True
    except Exception as e:
        logger.error(f"Failed to log chat question: {e}")
        return False
