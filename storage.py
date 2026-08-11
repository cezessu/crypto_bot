"""Persistent bot state and environment-based storage selection.

SQLite remains available for local development and tests.  Render can use a
Supabase Postgres database by setting ``SUPABASE_DATABASE_URL``.
"""

from __future__ import annotations

import sqlite3
import time
import os
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Optional, Tuple


class StorageError(Exception):
    """Base storage error safe to handle at the bot boundary."""


class MexcUidAlreadyBoundError(StorageError):
    """The requested MEXC UID belongs to another Telegram account."""


class UserMexcUidConflictError(StorageError):
    """A Telegram account is already bound to a different MEXC UID."""


class ReferralAssignment(str, Enum):
    ASSIGNED = "assigned"
    ALREADY_ASSIGNED = "already_assigned"
    SELF_REFERRAL = "self_referral"
    INVITER_NOT_FOUND = "inviter_not_found"


@dataclass(frozen=True)
class UserState:
    telegram_id: int
    mexc_uid: Optional[str]
    inviter_telegram_id: Optional[int]
    activity_confirmed_at: Optional[int]
    activity_baseline_last_trade_time: Optional[int]
    qualified_at: Optional[int]


class BotStorage:
    """Small transactional repository for users, referrals and issued lessons."""

    backend_name = "sqlite"

    def __init__(
        self,
        database_path: str,
        *,
        clock_ms: Optional[Callable[[], int]] = None,
    ) -> None:
        self.database_path = str(Path(database_path).expanduser())
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    mexc_uid TEXT UNIQUE,
                    inviter_telegram_id INTEGER,
                    activity_confirmed_at INTEGER,
                    activity_baseline_last_trade_time INTEGER,
                    qualified_at INTEGER,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    CHECK (
                        inviter_telegram_id IS NULL
                        OR inviter_telegram_id != telegram_id
                    ),
                    FOREIGN KEY (inviter_telegram_id)
                        REFERENCES users(telegram_id)
                );

                CREATE INDEX IF NOT EXISTS idx_users_inviter
                    ON users(inviter_telegram_id);

                CREATE INDEX IF NOT EXISTS idx_users_qualified
                    ON users(inviter_telegram_id, qualified_at);

                CREATE TABLE IF NOT EXISTS issued_lessons (
                    telegram_id INTEGER NOT NULL,
                    lesson_number INTEGER NOT NULL CHECK (lesson_number BETWEEN 1 AND 7),
                    issued_at INTEGER NOT NULL,
                    PRIMARY KEY (telegram_id, lesson_number),
                    FOREIGN KEY (telegram_id)
                        REFERENCES users(telegram_id)
                        ON DELETE CASCADE
                );
                """
            )

    def ensure_user(self, telegram_id: int) -> None:
        now = self.clock_ms()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO users (telegram_id, created_at, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(telegram_id) DO NOTHING
                """,
                (telegram_id, now, now),
            )

    def get_user(self, telegram_id: int) -> Optional[UserState]:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT telegram_id, mexc_uid, inviter_telegram_id,
                       activity_confirmed_at,
                       activity_baseline_last_trade_time,
                       qualified_at
                FROM users
                WHERE telegram_id = ?
                """,
                (telegram_id,),
            ).fetchone()
        if row is None:
            return None
        return UserState(
            telegram_id=row["telegram_id"],
            mexc_uid=row["mexc_uid"],
            inviter_telegram_id=row["inviter_telegram_id"],
            activity_confirmed_at=row["activity_confirmed_at"],
            activity_baseline_last_trade_time=row[
                "activity_baseline_last_trade_time"
            ],
            qualified_at=row["qualified_at"],
        )

    def assign_inviter(
        self,
        invited_telegram_id: int,
        inviter_telegram_id: int,
    ) -> ReferralAssignment:
        if invited_telegram_id == inviter_telegram_id:
            self.ensure_user(invited_telegram_id)
            return ReferralAssignment.SELF_REFERRAL

        now = self.clock_ms()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            inviter = connection.execute(
                "SELECT 1 FROM users WHERE telegram_id = ?",
                (inviter_telegram_id,),
            ).fetchone()
            if inviter is None:
                return ReferralAssignment.INVITER_NOT_FOUND

            connection.execute(
                """
                INSERT INTO users (telegram_id, created_at, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(telegram_id) DO NOTHING
                """,
                (invited_telegram_id, now, now),
            )
            row = connection.execute(
                "SELECT inviter_telegram_id FROM users WHERE telegram_id = ?",
                (invited_telegram_id,),
            ).fetchone()
            if row["inviter_telegram_id"] is not None:
                return ReferralAssignment.ALREADY_ASSIGNED

            connection.execute(
                """
                UPDATE users
                SET inviter_telegram_id = ?, updated_at = ?
                WHERE telegram_id = ? AND inviter_telegram_id IS NULL
                """,
                (inviter_telegram_id, now, invited_telegram_id),
            )
            return ReferralAssignment.ASSIGNED

    def bind_mexc_uid(self, telegram_id: int, mexc_uid: str) -> None:
        now = self.clock_ms()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO users (telegram_id, created_at, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(telegram_id) DO NOTHING
                """,
                (telegram_id, now, now),
            )
            current = connection.execute(
                "SELECT mexc_uid FROM users WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()["mexc_uid"]
            if current is not None and current != mexc_uid:
                raise UserMexcUidConflictError(
                    "Telegram account is already bound to another MEXC UID"
                )

            owner = connection.execute(
                "SELECT telegram_id FROM users WHERE mexc_uid = ?",
                (mexc_uid,),
            ).fetchone()
            if owner is not None and owner["telegram_id"] != telegram_id:
                raise MexcUidAlreadyBoundError(
                    "MEXC UID is already bound to another Telegram account"
                )

            connection.execute(
                "UPDATE users SET mexc_uid = ?, updated_at = ? WHERE telegram_id = ?",
                (mexc_uid, now, telegram_id),
            )

    def record_activity_confirmation(
        self,
        telegram_id: int,
        *,
        confirmed_at: int,
        baseline_last_trade_time: Optional[int],
    ) -> None:
        self.ensure_user(telegram_id)
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE users
                SET activity_confirmed_at = COALESCE(activity_confirmed_at, ?),
                    activity_baseline_last_trade_time = COALESCE(
                        activity_baseline_last_trade_time, ?
                    ),
                    updated_at = ?
                WHERE telegram_id = ?
                """,
                (
                    confirmed_at,
                    baseline_last_trade_time,
                    self.clock_ms(),
                    telegram_id,
                ),
            )

    def mark_qualified(self, telegram_id: int) -> None:
        now = self.clock_ms()
        with self._connection() as connection:
            updated = connection.execute(
                """
                UPDATE users
                SET qualified_at = COALESCE(qualified_at, ?), updated_at = ?
                WHERE telegram_id = ? AND mexc_uid IS NOT NULL
                """,
                (now, now, telegram_id),
            )
            if updated.rowcount != 1:
                raise StorageError("Cannot qualify a user without a bound MEXC UID")

    def count_qualified_invites(self, inviter_telegram_id: int) -> int:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM users
                WHERE inviter_telegram_id = ?
                  AND mexc_uid IS NOT NULL
                  AND qualified_at IS NOT NULL
                """,
                (inviter_telegram_id,),
            ).fetchone()
        return int(row["count"])

    def claim_lesson(self, telegram_id: int, lesson_number: int) -> bool:
        self.ensure_user(telegram_id)
        with self._connection() as connection:
            inserted = connection.execute(
                """
                INSERT INTO issued_lessons (telegram_id, lesson_number, issued_at)
                VALUES (?, ?, ?)
                ON CONFLICT(telegram_id, lesson_number) DO NOTHING
                """,
                (telegram_id, lesson_number, self.clock_ms()),
            )
        return inserted.rowcount == 1

    def release_lesson(self, telegram_id: int, lesson_number: int) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                DELETE FROM issued_lessons
                WHERE telegram_id = ? AND lesson_number = ?
                """,
                (telegram_id, lesson_number),
            )

    def is_lesson_issued(self, telegram_id: int, lesson_number: int) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM issued_lessons
                WHERE telegram_id = ? AND lesson_number = ?
                """,
                (telegram_id, lesson_number),
            ).fetchone()
        return row is not None

    def issued_lessons(self, telegram_id: int) -> Tuple[int, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT lesson_number FROM issued_lessons
                WHERE telegram_id = ?
                ORDER BY lesson_number
                """,
                (telegram_id,),
            ).fetchall()
        return tuple(int(row["lesson_number"]) for row in rows)


def create_storage_from_env(
    *,
    clock_ms: Optional[Callable[[], int]] = None,
):
    """Create Supabase Postgres storage when configured, otherwise SQLite.

    The connection string is never logged or embedded in code.  Importing the
    Postgres driver lazily keeps local SQLite-only tooling lightweight.
    """

    database_url = os.environ.get("SUPABASE_DATABASE_URL", "").strip()
    if database_url:
        from postgres_storage import PostgresStorage

        return PostgresStorage(database_url, clock_ms=clock_ms)

    database_path = os.environ.get(
        "DATABASE_PATH",
        "data/bot.sqlite3",
    )
    return BotStorage(database_path, clock_ms=clock_ms)
