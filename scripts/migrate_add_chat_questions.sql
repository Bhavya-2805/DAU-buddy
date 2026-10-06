-- =============================================================================
-- Migration: Add chat_questions table
-- Run on existing databases that were created before this table was added:
--   psql -U postgres -d <db_name> -f scripts/migrate_add_chat_questions.sql
-- =============================================================================

CREATE TABLE IF NOT EXISTS chat_questions (
    id            SERIAL PRIMARY KEY,
    question      TEXT NOT NULL,
    caller_role   VARCHAR(50) NOT NULL,
    created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_chat_questions_role
    ON chat_questions (caller_role);

CREATE INDEX IF NOT EXISTS idx_chat_questions_created
    ON chat_questions (created_at);
