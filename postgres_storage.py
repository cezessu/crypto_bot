"""Supabase Postgres implementation of the bot storage interface."""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Callable, Optional, Tuple

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError as exc:  # pragma: no cover - exercised only on broken deploys
    raise RuntimeError(
        "Postgres storage requires psycopg; install project requirements"
    ) from exc

from storage import (
    MexcUidAlreadyBoundError,
    ReferralAssignment,
    StorageError,
    UserMexcUidConflictError,
    UserState,
)


class PostgresStorage:
    """Transactional storage for Supabase Postgres.

    Supabase's transaction pooler does not support prepared statements, so
    psycopg's automatic preparation is disabled for every connection.
    """

    backend_name = "supabase-postgres"

    def __init__(
        self,
        database_url: str,
        *,
        clock_ms: Optional[Callable[[], int]] = None,
    ) -> None:
        database_url = database_url.strip()
        if not database_url.startswith(("postgres://", "postgresql://")):
            raise StorageError("SUPABASE_DATABASE_URL must be a Postgres URL")
        self._database_url = database_url
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._initialize()

    def _connect(self):
        try:
            return psycopg.connect(
                self._database_url,
                connect_timeout=10,
                sslmode="require",
                prepare_threshold=None,
                row_factory=dict_row,
            )
        except psycopg.Error as exc:
            raise StorageError("Persistent storage connection failed") from exc

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except (StorageError, MexcUidAlreadyBoundError, UserMexcUidConflictError):
            connection.rollback()
            raise
        except psycopg.Error as exc:
            connection.rollback()
            raise StorageError("Persistent storage operation failed") from exc
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_users (
                    telegram_id BIGINT PRIMARY KEY,
                    mexc_uid TEXT UNIQUE,
                    inviter_telegram_id BIGINT,
                    activity_confirmed_at BIGINT,
                    activity_baseline_last_trade_time BIGINT,
                    qualified_at BIGINT,
                    created_at BIGINT NOT NULL,
                    updated_at BIGINT NOT NULL,
                    CONSTRAINT bot_users_no_self_referral CHECK (
                        inviter_telegram_id IS NULL
                        OR inviter_telegram_id != telegram_id
                    ),
                    CONSTRAINT bot_users_inviter_fk FOREIGN KEY (
                        inviter_telegram_id
                    ) REFERENCES bot_users(telegram_id)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_bot_users_inviter
                ON bot_users(inviter_telegram_id)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_bot_users_qualified
                ON bot_users(inviter_telegram_id, qualified_at)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_issued_lessons (
                    telegram_id BIGINT NOT NULL,
                    lesson_number SMALLINT NOT NULL CHECK (
                        lesson_number BETWEEN 1 AND 7
                    ),
                    issued_at BIGINT NOT NULL,
                    PRIMARY KEY (telegram_id, lesson_number),
                    CONSTRAINT bot_issued_lessons_user_fk FOREIGN KEY (
                        telegram_id
                    ) REFERENCES bot_users(telegram_id) ON DELETE CASCADE
                )
                """
            )

    def ensure_user(self, telegram_id: int) -> None:
        now = self.clock_ms()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO bot_users (telegram_id, created_at, updated_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (telegram_id) DO NOTHING
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
                FROM bot_users
                WHERE telegram_id = %s
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
            inviter = connection.execute(
                "SELECT 1 FROM bot_users WHERE telegram_id = %s",
                (inviter_telegram_id,),
            ).fetchone()
            if inviter is None:
                return ReferralAssignment.INVITER_NOT_FOUND

            connection.execute(
                """
                INSERT INTO bot_users (telegram_id, created_at, updated_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (telegram_id) DO NOTHING
                """,
                (invited_telegram_id, now, now),
            )
            row = connection.execute(
                """
                SELECT inviter_telegram_id
                FROM bot_users
                WHERE telegram_id = %s
                FOR UPDATE
                """,
                (invited_telegram_id,),
            ).fetchone()
            if row["inviter_telegram_id"] is not None:
                return ReferralAssignment.ALREADY_ASSIGNED

            updated = connection.execute(
                """
                UPDATE bot_users
                SET inviter_telegram_id = %s, updated_at = %s
                WHERE telegram_id = %s AND inviter_telegram_id IS NULL
                """,
                (inviter_telegram_id, now, invited_telegram_id),
            )
            return (
                ReferralAssignment.ASSIGNED
                if updated.rowcount == 1
                else ReferralAssignment.ALREADY_ASSIGNED
            )

    def bind_mexc_uid(self, telegram_id: int, mexc_uid: str) -> None:
        now = self.clock_ms()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO bot_users (telegram_id, created_at, updated_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (telegram_id) DO NOTHING
                """,
                (telegram_id, now, now),
            )
            current = connection.execute(
                """
                SELECT mexc_uid FROM bot_users
                WHERE telegram_id = %s
                FOR UPDATE
                """,
                (telegram_id,),
            ).fetchone()["mexc_uid"]
            if current is not None and current != mexc_uid:
                raise UserMexcUidConflictError(
                    "Telegram account is already bound to another MEXC UID"
                )

            owner = connection.execute(
                "SELECT telegram_id FROM bot_users WHERE mexc_uid = %s",
                (mexc_uid,),
            ).fetchone()
            if owner is not None and owner["telegram_id"] != telegram_id:
                raise MexcUidAlreadyBoundError(
                    "MEXC UID is already bound to another Telegram account"
                )

            try:
                connection.execute(
                    """
                    UPDATE bot_users
                    SET mexc_uid = %s, updated_at = %s
                    WHERE telegram_id = %s
                    """,
                    (mexc_uid, now, telegram_id),
                )
            except psycopg.errors.UniqueViolation as exc:
                raise MexcUidAlreadyBoundError(
                    "MEXC UID is already bound to another Telegram account"
                ) from exc

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
                UPDATE bot_users
                SET activity_confirmed_at = COALESCE(
                        activity_confirmed_at, %s
                    ),
                    activity_baseline_last_trade_time = COALESCE(
                        activity_baseline_last_trade_time, %s
                    ),
                    updated_at = %s
                WHERE telegram_id = %s
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
                UPDATE bot_users
                SET qualified_at = COALESCE(qualified_at, %s),
                    updated_at = %s
                WHERE telegram_id = %s AND mexc_uid IS NOT NULL
                """,
                (now, now, telegram_id),
            )
            if updated.rowcount != 1:
                raise StorageError(
                    "Cannot qualify a user without a bound MEXC UID"
                )

    def count_qualified_invites(self, inviter_telegram_id: int) -> int:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM bot_users
                WHERE inviter_telegram_id = %s
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
                INSERT INTO bot_issued_lessons (
                    telegram_id, lesson_number, issued_at
                ) VALUES (%s, %s, %s)
                ON CONFLICT (telegram_id, lesson_number) DO NOTHING
                RETURNING 1
                """,
                (telegram_id, lesson_number, self.clock_ms()),
            ).fetchone()
        return inserted is not None

    def release_lesson(self, telegram_id: int, lesson_number: int) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                DELETE FROM bot_issued_lessons
                WHERE telegram_id = %s AND lesson_number = %s
                """,
                (telegram_id, lesson_number),
            )

    def is_lesson_issued(self, telegram_id: int, lesson_number: int) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM bot_issued_lessons
                WHERE telegram_id = %s AND lesson_number = %s
                """,
                (telegram_id, lesson_number),
            ).fetchone()
        return row is not None

    def issued_lessons(self, telegram_id: int) -> Tuple[int, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT lesson_number FROM bot_issued_lessons
                WHERE telegram_id = %s
                ORDER BY lesson_number
                """,
                (telegram_id,),
            ).fetchall()
        return tuple(int(row["lesson_number"]) for row in rows)
