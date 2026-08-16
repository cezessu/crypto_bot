import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import ANY, patch

import psycopg

from postgres_storage import PostgresStorage
from storage import BotStorage, StorageError, create_storage_from_env


class FakeCursor:
    rowcount = 1

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class FakeConnection:
    def __init__(self):
        self.statements = []
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def execute(self, statement, params=None):
        self.statements.append((statement, params))
        return FakeCursor()

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class StorageFactoryTests(unittest.TestCase):
    def test_sqlite_is_used_for_local_development_without_supabase_url(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {
                "SUPABASE_DATABASE_URL": "",
                "DATABASE_PATH": str(Path(temp_dir) / "state.sqlite3"),
            },
            clear=False,
        ):
            storage = create_storage_from_env()

        self.assertIsInstance(storage, BotStorage)
        self.assertEqual(storage.backend_name, "sqlite")

    def test_supabase_url_selects_postgres_and_disables_prepared_statements(self):
        connection = FakeConnection()
        database_url = (
            "postgresql://postgres.project:password@"
            "aws-0-region.pooler.supabase.com:6543/postgres"
        )
        with patch.dict(
            os.environ,
            {"SUPABASE_DATABASE_URL": database_url},
            clear=False,
        ), patch("postgres_storage.psycopg.connect", return_value=connection) as connect:
            storage = create_storage_from_env()

        self.assertIsInstance(storage, PostgresStorage)
        self.assertEqual(storage.backend_name, "supabase-postgres")
        connect.assert_called_once_with(
            database_url,
            connect_timeout=10,
            sslmode="require",
            prepare_threshold=None,
            row_factory=ANY,
        )
        self.assertTrue(connection.committed)
        self.assertTrue(connection.closed)
        schema = "\n".join(statement for statement, _ in connection.statements)
        self.assertIn("CREATE TABLE IF NOT EXISTS bot_users", schema)
        self.assertIn("CREATE TABLE IF NOT EXISTS bot_issued_lessons", schema)
        self.assertIn("CREATE TABLE IF NOT EXISTS bot_lesson_delivery_claims", schema)
        self.assertIn("CREATE TABLE IF NOT EXISTS bot_lesson_delivery_parts", schema)
        self.assertIn("CREATE TABLE IF NOT EXISTS bot_lesson_review_requests", schema)
        self.assertIn(
            "CREATE TABLE IF NOT EXISTS bot_lesson_review_notification_claims",
            schema,
        )
        self.assertIn("lesson_number IN (3, 7)", schema)
        self.assertIn("idx_bot_lesson_review_requests_active", schema)
        self.assertIn(
            "ALTER TABLE bot_users ENABLE ROW LEVEL SECURITY",
            schema,
        )
        self.assertIn(
            "ALTER TABLE bot_issued_lessons ENABLE ROW LEVEL SECURITY",
            schema,
        )
        self.assertIn(
            "ALTER TABLE bot_lesson_delivery_claims ENABLE ROW LEVEL SECURITY",
            schema,
        )
        self.assertIn(
            "ALTER TABLE bot_lesson_delivery_parts ENABLE ROW LEVEL SECURITY",
            schema,
        )
        self.assertIn(
            "ALTER TABLE bot_lesson_review_requests ENABLE ROW LEVEL SECURITY",
            schema,
        )
        self.assertIn(
            "ALTER TABLE bot_lesson_review_notification_claims ENABLE ROW LEVEL SECURITY",
            schema,
        )
        self.assertIn(
            "REVOKE ALL ON TABLE bot_users FROM anon, authenticated",
            schema,
        )

    def test_invalid_supabase_url_is_rejected(self):
        with self.assertRaises(StorageError):
            PostgresStorage("https://example.supabase.co")

    def test_connection_failure_is_exposed_as_safe_storage_error(self):
        with patch(
            "postgres_storage.psycopg.connect",
            side_effect=psycopg.OperationalError("connection failed"),
        ):
            with self.assertRaisesRegex(
                StorageError,
                "Persistent storage connection failed",
            ):
                PostgresStorage(
                    "postgresql://postgres.project:password@"
                    "aws-0-region.pooler.supabase.com:6543/postgres"
                )

    def test_postgres_backend_implements_bot_storage_interface(self):
        required_methods = {
            "ensure_user",
            "get_user",
            "assign_inviter",
            "bind_mexc_uid",
            "record_activity_confirmation",
            "mark_qualified",
            "count_qualified_invites",
            "claim_lesson",
            "claim_lesson_delivery",
            "renew_lesson_delivery",
            "complete_lesson_delivery",
            "release_lesson_delivery",
            "release_lesson",
            "is_lesson_part_delivered",
            "mark_lesson_part_delivered",
            "mark_claimed_lesson_part_delivered",
            "is_lesson_issued",
            "is_lesson_delivery_in_progress",
            "issued_lessons",
            "request_lesson_review",
            "get_lesson_review_request",
            "mark_lesson_review_notified",
            "mark_lesson_review_user_notified",
            "claim_lesson_review_notification",
            "complete_lesson_review_notification",
            "release_lesson_review_notification",
            "decide_lesson_review_request",
            "claim_lesson_review_delivery",
            "release_lesson_review_delivery",
            "mark_lesson_review_fulfilled",
        }
        for method_name in required_methods:
            with self.subTest(method=method_name):
                self.assertTrue(callable(getattr(PostgresStorage, method_name)))


if __name__ == "__main__":
    unittest.main()
