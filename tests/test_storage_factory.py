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
            "release_lesson",
            "is_lesson_issued",
            "issued_lessons",
        }
        for method_name in required_methods:
            with self.subTest(method=method_name):
                self.assertTrue(callable(getattr(PostgresStorage, method_name)))


if __name__ == "__main__":
    unittest.main()
