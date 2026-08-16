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
    LessonDeliveryClaimStatus,
    LessonAlreadyIssuedError,
    LessonDeliveryInProgressError,
    LessonReviewDecision,
    LessonReviewRequest,
    LessonReviewStatus,
    LessonReviewSubmission,
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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_lesson_delivery_claims (
                    telegram_id BIGINT NOT NULL,
                    lesson_number SMALLINT NOT NULL CHECK (
                        lesson_number BETWEEN 1 AND 7
                    ),
                    delivery_token TEXT NOT NULL,
                    claimed_at BIGINT NOT NULL,
                    lease_expires_at BIGINT NOT NULL,
                    PRIMARY KEY (telegram_id, lesson_number),
                    CONSTRAINT bot_lesson_delivery_claims_user_fk FOREIGN KEY (
                        telegram_id
                    ) REFERENCES bot_users(telegram_id) ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_lesson_delivery_parts (
                    telegram_id BIGINT NOT NULL,
                    lesson_number SMALLINT NOT NULL CHECK (
                        lesson_number BETWEEN 1 AND 7
                    ),
                    part TEXT NOT NULL CHECK (part IN ('main', 'bonus')),
                    delivered_at BIGINT NOT NULL,
                    PRIMARY KEY (telegram_id, lesson_number, part),
                    CONSTRAINT bot_lesson_delivery_parts_user_fk FOREIGN KEY (
                        telegram_id
                    ) REFERENCES bot_users(telegram_id) ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_lesson_review_requests (
                    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    telegram_id BIGINT NOT NULL,
                    lesson_number SMALLINT NOT NULL CHECK (
                        lesson_number IN (3, 7)
                    ),
                    mexc_uid TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN (
                            'pending', 'approved', 'rejected', 'fulfilled'
                        )
                    ),
                    requested_at BIGINT NOT NULL,
                    updated_at BIGINT NOT NULL,
                    admin_notified_at BIGINT,
                    user_notified_at BIGINT,
                    decided_at BIGINT,
                    decided_by BIGINT,
                    fulfilled_at BIGINT,
                    delivery_token TEXT,
                    delivery_claimed_at BIGINT,
                    delivery_lease_expires_at BIGINT,
                    CONSTRAINT bot_lesson_review_requests_user_fk FOREIGN KEY (
                        telegram_id
                    ) REFERENCES bot_users(telegram_id) ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_bot_lesson_review_requests_user
                ON bot_lesson_review_requests(telegram_id)
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_bot_lesson_review_requests_active
                ON bot_lesson_review_requests(telegram_id, lesson_number)
                WHERE status IN ('pending', 'approved')
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_lesson_review_notification_claims (
                    request_id BIGINT NOT NULL,
                    recipient TEXT NOT NULL CHECK (recipient IN ('admin', 'user')),
                    notification_token TEXT NOT NULL,
                    claimed_at BIGINT NOT NULL,
                    lease_expires_at BIGINT NOT NULL,
                    PRIMARY KEY (request_id, recipient),
                    CONSTRAINT bot_lesson_review_notification_claims_request_fk
                        FOREIGN KEY (request_id)
                        REFERENCES bot_lesson_review_requests(id)
                        ON DELETE CASCADE
                )
                """
            )
            # The bot connects as the database owner and bypasses RLS. Keeping
            # these internal tables policy-free prevents Telegram/MEXC data
            # from being exposed through Supabase's anon/authenticated roles.
            for table_name in (
                "bot_users",
                "bot_issued_lessons",
                "bot_lesson_delivery_claims",
                "bot_lesson_delivery_parts",
                "bot_lesson_review_requests",
                "bot_lesson_review_notification_claims",
            ):
                connection.execute(
                    f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY"
                )
                connection.execute(
                    f"REVOKE ALL ON TABLE {table_name} FROM anon, authenticated"
                )

    @staticmethod
    def _lesson_review_from_row(row) -> LessonReviewRequest:
        return LessonReviewRequest(
            request_id=int(row["id"]),
            telegram_id=int(row["telegram_id"]),
            lesson_number=int(row["lesson_number"]),
            mexc_uid=str(row["mexc_uid"]),
            status=LessonReviewStatus(row["status"]),
            requested_at=int(row["requested_at"]),
            updated_at=int(row["updated_at"]),
            admin_notified_at=row["admin_notified_at"],
            user_notified_at=row["user_notified_at"],
            decided_at=row["decided_at"],
            decided_by=row["decided_by"],
            fulfilled_at=row["fulfilled_at"],
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

    def claim_lesson_delivery(
        self,
        telegram_id: int,
        lesson_number: int,
        delivery_token: str,
        lease_ms: int,
    ) -> LessonDeliveryClaimStatus:
        if not 1 <= lesson_number <= 7:
            raise ValueError("Lesson number must be between 1 and 7")
        if not delivery_token or lease_ms <= 0:
            raise ValueError("A delivery claim requires a token and positive lease")

        self.ensure_user(telegram_id)
        now = self.clock_ms()
        with self._connection() as connection:
            connection.execute(
                "SELECT telegram_id FROM bot_users WHERE telegram_id = %s FOR UPDATE",
                (telegram_id,),
            ).fetchone()
            issued = connection.execute(
                """
                SELECT 1 FROM bot_issued_lessons
                WHERE telegram_id = %s AND lesson_number = %s
                """,
                (telegram_id, lesson_number),
            ).fetchone()
            if issued is not None:
                return LessonDeliveryClaimStatus.ALREADY_ISSUED

            rejection_notice = connection.execute(
                """
                SELECT 1
                FROM bot_lesson_review_requests AS review
                JOIN bot_lesson_review_notification_claims AS notification
                  ON notification.request_id = review.id
                WHERE review.telegram_id = %s AND review.lesson_number = %s
                  AND review.status = 'rejected'
                  AND notification.recipient = 'user'
                  AND notification.lease_expires_at > %s
                """,
                (telegram_id, lesson_number, now),
            ).fetchone()
            if rejection_notice is not None:
                return LessonDeliveryClaimStatus.BUSY

            current = connection.execute(
                """
                SELECT lease_expires_at FROM bot_lesson_delivery_claims
                WHERE telegram_id = %s AND lesson_number = %s
                """,
                (telegram_id, lesson_number),
            ).fetchone()
            if current is not None and current["lease_expires_at"] > now:
                return LessonDeliveryClaimStatus.BUSY

            connection.execute(
                """
                INSERT INTO bot_lesson_delivery_claims (
                    telegram_id, lesson_number, delivery_token,
                    claimed_at, lease_expires_at
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (telegram_id, lesson_number) DO UPDATE SET
                    delivery_token = excluded.delivery_token,
                    claimed_at = excluded.claimed_at,
                    lease_expires_at = excluded.lease_expires_at
                """,
                (
                    telegram_id,
                    lesson_number,
                    delivery_token,
                    now,
                    now + lease_ms,
                ),
            )
        return LessonDeliveryClaimStatus.ACQUIRED

    def renew_lesson_delivery(
        self,
        telegram_id: int,
        lesson_number: int,
        delivery_token: str,
        lease_ms: int,
    ) -> bool:
        if not delivery_token or lease_ms <= 0:
            raise ValueError("A delivery renewal requires a token and positive lease")
        now = self.clock_ms()
        with self._connection() as connection:
            connection.execute(
                "SELECT telegram_id FROM bot_users WHERE telegram_id = %s FOR UPDATE",
                (telegram_id,),
            ).fetchone()
            updated = connection.execute(
                """
                UPDATE bot_lesson_delivery_claims
                SET claimed_at = %s, lease_expires_at = %s
                WHERE telegram_id = %s AND lesson_number = %s
                  AND delivery_token = %s AND lease_expires_at > %s
                """,
                (
                    now,
                    now + lease_ms,
                    telegram_id,
                    lesson_number,
                    delivery_token,
                    now,
                ),
            )
        return updated.rowcount == 1

    def complete_lesson_delivery(
        self,
        telegram_id: int,
        lesson_number: int,
        delivery_token: str,
    ) -> bool:
        now = self.clock_ms()
        with self._connection() as connection:
            connection.execute(
                "SELECT telegram_id FROM bot_users WHERE telegram_id = %s FOR UPDATE",
                (telegram_id,),
            ).fetchone()
            claim = connection.execute(
                """
                SELECT 1 FROM bot_lesson_delivery_claims
                WHERE telegram_id = %s AND lesson_number = %s
                  AND delivery_token = %s AND lease_expires_at > %s
                """,
                (telegram_id, lesson_number, delivery_token, now),
            ).fetchone()
            if claim is None:
                return False
            connection.execute(
                """
                INSERT INTO bot_issued_lessons (
                    telegram_id, lesson_number, issued_at
                ) VALUES (%s, %s, %s)
                ON CONFLICT (telegram_id, lesson_number) DO NOTHING
                """,
                (telegram_id, lesson_number, now),
            )
            connection.execute(
                """
                UPDATE bot_lesson_review_requests
                SET status = 'fulfilled', fulfilled_at = %s, updated_at = %s,
                    delivery_token = NULL, delivery_claimed_at = NULL,
                    delivery_lease_expires_at = NULL
                WHERE telegram_id = %s AND lesson_number = %s
                  AND status IN ('pending', 'approved')
                """,
                (now, now, telegram_id, lesson_number),
            )
            connection.execute(
                """
                DELETE FROM bot_lesson_delivery_claims
                WHERE telegram_id = %s AND lesson_number = %s
                  AND delivery_token = %s
                """,
                (telegram_id, lesson_number, delivery_token),
            )
        return True

    def release_lesson_delivery(
        self,
        telegram_id: int,
        lesson_number: int,
        delivery_token: str,
    ) -> bool:
        with self._connection() as connection:
            deleted = connection.execute(
                """
                DELETE FROM bot_lesson_delivery_claims
                WHERE telegram_id = %s AND lesson_number = %s
                  AND delivery_token = %s
                """,
                (telegram_id, lesson_number, delivery_token),
            )
        return deleted.rowcount == 1

    def release_lesson(self, telegram_id: int, lesson_number: int) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                DELETE FROM bot_issued_lessons
                WHERE telegram_id = %s AND lesson_number = %s
                """,
                (telegram_id, lesson_number),
            )

    @staticmethod
    def _validate_lesson_part(part: str) -> None:
        if part not in ("main", "bonus"):
            raise ValueError("Lesson part must be main or bonus")

    def is_lesson_part_delivered(
        self,
        telegram_id: int,
        lesson_number: int,
        part: str,
    ) -> bool:
        self._validate_lesson_part(part)
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM bot_lesson_delivery_parts
                WHERE telegram_id = %s AND lesson_number = %s AND part = %s
                """,
                (telegram_id, lesson_number, part),
            ).fetchone()
        return row is not None

    def mark_lesson_part_delivered(
        self,
        telegram_id: int,
        lesson_number: int,
        part: str,
    ) -> bool:
        self._validate_lesson_part(part)
        with self._connection() as connection:
            inserted = connection.execute(
                """
                INSERT INTO bot_lesson_delivery_parts (
                    telegram_id, lesson_number, part, delivered_at
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT (telegram_id, lesson_number, part) DO NOTHING
                RETURNING 1
                """,
                (telegram_id, lesson_number, part, self.clock_ms()),
            ).fetchone()
        return inserted is not None

    def mark_claimed_lesson_part_delivered(
        self,
        telegram_id: int,
        lesson_number: int,
        part: str,
        delivery_token: str,
    ) -> bool:
        self._validate_lesson_part(part)
        now = self.clock_ms()
        with self._connection() as connection:
            connection.execute(
                "SELECT telegram_id FROM bot_users WHERE telegram_id = %s FOR UPDATE",
                (telegram_id,),
            ).fetchone()
            claim = connection.execute(
                """
                SELECT 1 FROM bot_lesson_delivery_claims
                WHERE telegram_id = %s AND lesson_number = %s
                  AND delivery_token = %s AND lease_expires_at > %s
                """,
                (telegram_id, lesson_number, delivery_token, now),
            ).fetchone()
            if claim is None:
                return False
            inserted = connection.execute(
                """
                INSERT INTO bot_lesson_delivery_parts (
                    telegram_id, lesson_number, part, delivered_at
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT (telegram_id, lesson_number, part) DO NOTHING
                RETURNING 1
                """,
                (telegram_id, lesson_number, part, now),
            ).fetchone()
        return inserted is not None

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

    def is_lesson_delivery_in_progress(
        self,
        telegram_id: int,
        lesson_number: int,
    ) -> bool:
        now = self.clock_ms()
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM bot_lesson_delivery_claims
                WHERE telegram_id = %s AND lesson_number = %s
                  AND lease_expires_at > %s
                """,
                (telegram_id, lesson_number, now),
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

    def request_lesson_review(
        self,
        telegram_id: int,
        lesson_number: int,
    ) -> LessonReviewSubmission:
        if lesson_number not in (3, 7):
            raise ValueError("Manual review is supported only for lessons 3 and 7")

        self.ensure_user(telegram_id)
        now = self.clock_ms()
        with self._connection() as connection:
            user = connection.execute(
                """
                SELECT mexc_uid FROM bot_users
                WHERE telegram_id = %s
                FOR UPDATE
                """,
                (telegram_id,),
            ).fetchone()
            if user is None or user["mexc_uid"] is None:
                raise StorageError("Manual review requires a bound MEXC UID")

            issued = connection.execute(
                """
                SELECT 1 FROM bot_issued_lessons
                WHERE telegram_id = %s AND lesson_number = %s
                """,
                (telegram_id, lesson_number),
            ).fetchone()
            if issued is not None:
                raise LessonAlreadyIssuedError("Lesson has already been issued")

            delivery = connection.execute(
                """
                SELECT 1 FROM bot_lesson_delivery_claims
                WHERE telegram_id = %s AND lesson_number = %s
                  AND lease_expires_at > %s
                """,
                (telegram_id, lesson_number, now),
            ).fetchone()
            if delivery is not None:
                raise LessonDeliveryInProgressError(
                    "Lesson delivery is already in progress"
                )

            row = connection.execute(
                """
                INSERT INTO bot_lesson_review_requests (
                    telegram_id, lesson_number, mexc_uid, status,
                    requested_at, updated_at
                ) VALUES (%s, %s, %s, 'pending', %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING id, telegram_id, lesson_number, mexc_uid, status,
                          requested_at, updated_at, admin_notified_at, user_notified_at,
                          decided_at, decided_by, fulfilled_at
                """,
                (telegram_id, lesson_number, user["mexc_uid"], now, now),
            ).fetchone()
            created = row is not None
            if row is None:
                row = connection.execute(
                    """
                    SELECT id, telegram_id, lesson_number, mexc_uid, status,
                           requested_at, updated_at, admin_notified_at, user_notified_at,
                           decided_at, decided_by, fulfilled_at
                    FROM bot_lesson_review_requests
                    WHERE telegram_id = %s AND lesson_number = %s
                      AND status IN ('pending', 'approved')
                    """,
                    (telegram_id, lesson_number),
                ).fetchone()
        if row is None:
            raise StorageError("Could not create or load a lesson review request")
        return LessonReviewSubmission(
            request=self._lesson_review_from_row(row),
            created=created,
        )

    def get_lesson_review_request(
        self,
        request_id: int,
    ) -> Optional[LessonReviewRequest]:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT id, telegram_id, lesson_number, mexc_uid, status,
                       requested_at, updated_at, admin_notified_at, user_notified_at,
                       decided_at, decided_by, fulfilled_at
                FROM bot_lesson_review_requests
                WHERE id = %s
                """,
                (request_id,),
            ).fetchone()
        return None if row is None else self._lesson_review_from_row(row)

    def mark_lesson_review_notified(self, request_id: int) -> bool:
        now = self.clock_ms()
        with self._connection() as connection:
            updated = connection.execute(
                """
                UPDATE bot_lesson_review_requests
                SET admin_notified_at = COALESCE(admin_notified_at, %s),
                    updated_at = %s
                WHERE id = %s AND status IN ('pending', 'approved')
                """,
                (now, now, request_id),
            )
        return updated.rowcount == 1

    def mark_lesson_review_user_notified(self, request_id: int) -> bool:
        now = self.clock_ms()
        with self._connection() as connection:
            updated = connection.execute(
                """
                UPDATE bot_lesson_review_requests
                SET user_notified_at = COALESCE(user_notified_at, %s),
                    updated_at = %s
                WHERE id = %s AND status = 'rejected'
                """,
                (now, now, request_id),
            )
        return updated.rowcount == 1

    @staticmethod
    def _validate_review_notification_recipient(recipient: str) -> None:
        if recipient not in ("admin", "user"):
            raise ValueError("A review notification recipient must be admin or user")

    def claim_lesson_review_notification(
        self,
        request_id: int,
        recipient: str,
        notification_token: str,
        lease_ms: int,
    ) -> bool:
        self._validate_review_notification_recipient(recipient)
        if not notification_token or lease_ms <= 0:
            raise ValueError("A notification claim requires a token and positive lease")

        now = self.clock_ms()
        with self._connection() as connection:
            review_seed = connection.execute(
                """
                SELECT telegram_id
                FROM bot_lesson_review_requests
                WHERE id = %s
                """,
                (request_id,),
            ).fetchone()
            if review_seed is None:
                return False
            connection.execute(
                """
                SELECT telegram_id FROM bot_users
                WHERE telegram_id = %s
                FOR UPDATE
                """,
                (review_seed["telegram_id"],),
            ).fetchone()
            review = connection.execute(
                """
                SELECT telegram_id, lesson_number, status,
                       admin_notified_at, user_notified_at
                FROM bot_lesson_review_requests
                WHERE id = %s
                FOR UPDATE
                """,
                (request_id,),
            ).fetchone()
            if review is None:
                return False

            eligible = (
                recipient == "admin"
                and review["status"] in (
                    LessonReviewStatus.PENDING.value,
                    LessonReviewStatus.APPROVED.value,
                )
                and review["admin_notified_at"] is None
            ) or (
                recipient == "user"
                and review["status"] == LessonReviewStatus.REJECTED.value
                and review["user_notified_at"] is None
            )
            if not eligible:
                return False

            if recipient == "user":
                delivery = connection.execute(
                    """
                    SELECT 1 FROM bot_lesson_delivery_claims
                    WHERE telegram_id = %s AND lesson_number = %s
                      AND lease_expires_at > %s
                    """,
                    (review["telegram_id"], review["lesson_number"], now),
                ).fetchone()
                if delivery is not None:
                    return False

            claimed = connection.execute(
                """
                INSERT INTO bot_lesson_review_notification_claims (
                    request_id, recipient, notification_token,
                    claimed_at, lease_expires_at
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (request_id, recipient) DO UPDATE
                SET notification_token = EXCLUDED.notification_token,
                    claimed_at = EXCLUDED.claimed_at,
                    lease_expires_at = EXCLUDED.lease_expires_at
                WHERE bot_lesson_review_notification_claims.lease_expires_at <= %s
                RETURNING request_id
                """,
                (
                    request_id,
                    recipient,
                    notification_token,
                    now,
                    now + lease_ms,
                    now,
                ),
            ).fetchone()
        return claimed is not None

    def complete_lesson_review_notification(
        self,
        request_id: int,
        recipient: str,
        notification_token: str,
    ) -> bool:
        self._validate_review_notification_recipient(recipient)
        now = self.clock_ms()
        notified_column = (
            "admin_notified_at" if recipient == "admin" else "user_notified_at"
        )
        status_clause = (
            "status IN ('pending', 'approved')"
            if recipient == "admin"
            else "status = 'rejected'"
        )
        with self._connection() as connection:
            review_seed = connection.execute(
                """
                SELECT telegram_id
                FROM bot_lesson_review_requests
                WHERE id = %s
                """,
                (request_id,),
            ).fetchone()
            if review_seed is None:
                return False
            connection.execute(
                """
                SELECT telegram_id FROM bot_users
                WHERE telegram_id = %s
                FOR UPDATE
                """,
                (review_seed["telegram_id"],),
            ).fetchone()
            # All review/delivery paths lock user -> request -> claim.
            review = connection.execute(
                """
                SELECT id FROM bot_lesson_review_requests
                WHERE id = %s
                FOR UPDATE
                """,
                (request_id,),
            ).fetchone()
            if review is None:
                return False
            claim = connection.execute(
                """
                SELECT notification_token
                FROM bot_lesson_review_notification_claims
                WHERE request_id = %s AND recipient = %s
                FOR UPDATE
                """,
                (request_id, recipient),
            ).fetchone()
            if claim is None or claim["notification_token"] != notification_token:
                return False
            updated = connection.execute(
                f"""
                UPDATE bot_lesson_review_requests
                SET {notified_column} = COALESCE({notified_column}, %s),
                    updated_at = %s
                WHERE id = %s AND {status_clause}
                """,
                (now, now, request_id),
            )
            connection.execute(
                """
                DELETE FROM bot_lesson_review_notification_claims
                WHERE request_id = %s AND recipient = %s
                  AND notification_token = %s
                """,
                (request_id, recipient, notification_token),
            )
        return updated.rowcount == 1

    def release_lesson_review_notification(
        self,
        request_id: int,
        recipient: str,
        notification_token: str,
    ) -> bool:
        self._validate_review_notification_recipient(recipient)
        with self._connection() as connection:
            deleted = connection.execute(
                """
                DELETE FROM bot_lesson_review_notification_claims
                WHERE request_id = %s AND recipient = %s
                  AND notification_token = %s
                """,
                (request_id, recipient, notification_token),
            )
        return deleted.rowcount == 1

    def decide_lesson_review_request(
        self,
        request_id: int,
        status: LessonReviewStatus,
        reviewer_telegram_id: int,
        *,
        notification_token: Optional[str] = None,
        notification_lease_ms: int = 0,
    ) -> LessonReviewDecision:
        if status not in (LessonReviewStatus.APPROVED, LessonReviewStatus.REJECTED):
            raise ValueError("A review decision must be approved or rejected")
        if notification_token is not None and notification_lease_ms <= 0:
            raise ValueError("A rejection notification claim requires a positive lease")

        now = self.clock_ms()
        with self._connection() as connection:
            review_seed = connection.execute(
                """
                SELECT telegram_id
                FROM bot_lesson_review_requests
                WHERE id = %s
                """,
                (request_id,),
            ).fetchone()
            if review_seed is None:
                return LessonReviewDecision(request=None, changed=False)
            connection.execute(
                """
                SELECT telegram_id FROM bot_users
                WHERE telegram_id = %s
                FOR UPDATE
                """,
                (review_seed["telegram_id"],),
            ).fetchone()
            row = connection.execute(
                """
                SELECT id, telegram_id, lesson_number, mexc_uid, status,
                       requested_at, updated_at, admin_notified_at, user_notified_at,
                       decided_at, decided_by, fulfilled_at
                FROM bot_lesson_review_requests
                WHERE id = %s
                FOR UPDATE
                """,
                (request_id,),
            ).fetchone()
            if row is None:
                return LessonReviewDecision(request=None, changed=False)

            can_reconcile_delivery = row["status"] in (
                LessonReviewStatus.PENDING.value,
                LessonReviewStatus.APPROVED.value,
            ) or (
                row["status"] == LessonReviewStatus.REJECTED.value
                and row["user_notified_at"] is None
            )
            if can_reconcile_delivery:
                issued = connection.execute(
                    """
                    SELECT 1 FROM bot_issued_lessons
                    WHERE telegram_id = %s AND lesson_number = %s
                    """,
                    (row["telegram_id"], row["lesson_number"]),
                ).fetchone()
                if issued is not None:
                    if row["status"] == LessonReviewStatus.REJECTED.value:
                        return LessonReviewDecision(
                            request=self._lesson_review_from_row(row),
                            changed=False,
                            lesson_already_issued=True,
                        )
                    row = connection.execute(
                        """
                        UPDATE bot_lesson_review_requests
                        SET status = 'fulfilled', fulfilled_at = %s, updated_at = %s
                        WHERE id = %s AND status IN ('pending', 'approved')
                        RETURNING id, telegram_id, lesson_number, mexc_uid, status,
                                  requested_at, updated_at, admin_notified_at,
                                  user_notified_at, decided_at, decided_by, fulfilled_at
                        """,
                        (now, now, request_id),
                    ).fetchone()
                    return LessonReviewDecision(
                        request=self._lesson_review_from_row(row),
                        changed=False,
                    )

                delivery = connection.execute(
                    """
                    SELECT 1 FROM bot_lesson_delivery_claims
                    WHERE telegram_id = %s AND lesson_number = %s
                      AND lease_expires_at > %s
                    """,
                    (row["telegram_id"], row["lesson_number"], now),
                ).fetchone()
                if delivery is not None:
                    return LessonReviewDecision(
                        request=self._lesson_review_from_row(row),
                        changed=False,
                        delivery_in_progress=True,
                    )

            changed = row["status"] == LessonReviewStatus.PENDING.value
            if changed:
                row = connection.execute(
                    """
                    UPDATE bot_lesson_review_requests
                    SET status = %s, decided_by = %s, decided_at = %s,
                        updated_at = %s
                    WHERE id = %s AND status = 'pending'
                    RETURNING id, telegram_id, lesson_number, mexc_uid, status,
                              requested_at, updated_at, admin_notified_at, user_notified_at,
                              decided_at, decided_by, fulfilled_at
                    """,
                    (status.value, reviewer_telegram_id, now, now, request_id),
                ).fetchone()
            notification_claimed = False
            if (
                notification_token is not None
                and row["status"] == LessonReviewStatus.REJECTED.value
                and row["user_notified_at"] is None
            ):
                claimed = connection.execute(
                    """
                    INSERT INTO bot_lesson_review_notification_claims (
                        request_id, recipient, notification_token,
                        claimed_at, lease_expires_at
                    ) VALUES (%s, 'user', %s, %s, %s)
                    ON CONFLICT (request_id, recipient) DO UPDATE
                    SET notification_token = EXCLUDED.notification_token,
                        claimed_at = EXCLUDED.claimed_at,
                        lease_expires_at = EXCLUDED.lease_expires_at
                    WHERE bot_lesson_review_notification_claims.lease_expires_at <= %s
                    RETURNING request_id
                    """,
                    (
                        request_id,
                        notification_token,
                        now,
                        now + notification_lease_ms,
                        now,
                    ),
                ).fetchone()
                notification_claimed = claimed is not None
        return LessonReviewDecision(
            request=self._lesson_review_from_row(row),
            changed=changed,
            notification_claimed=notification_claimed,
        )

    def claim_lesson_review_delivery(
        self,
        request_id: int,
        delivery_token: str,
        lease_ms: int,
    ) -> bool:
        if not delivery_token or lease_ms <= 0:
            raise ValueError("A delivery claim requires a token and positive lease")

        now = self.clock_ms()
        with self._connection() as connection:
            updated = connection.execute(
                """
                UPDATE bot_lesson_review_requests
                SET delivery_token = %s, delivery_claimed_at = %s,
                    delivery_lease_expires_at = %s, updated_at = %s
                WHERE id = %s AND status = 'approved'
                  AND (
                      delivery_token IS NULL
                      OR delivery_lease_expires_at <= %s
                  )
                """,
                (delivery_token, now, now + lease_ms, now, request_id, now),
            )
        return updated.rowcount == 1

    def release_lesson_review_delivery(
        self,
        request_id: int,
        delivery_token: str,
    ) -> bool:
        with self._connection() as connection:
            updated = connection.execute(
                """
                UPDATE bot_lesson_review_requests
                SET delivery_token = NULL, delivery_claimed_at = NULL,
                    delivery_lease_expires_at = NULL, updated_at = %s
                WHERE id = %s AND status = 'approved' AND delivery_token = %s
                """,
                (self.clock_ms(), request_id, delivery_token),
            )
        return updated.rowcount == 1

    def mark_lesson_review_fulfilled(
        self,
        request_id: int,
        delivery_token: str,
    ) -> bool:
        now = self.clock_ms()
        with self._connection() as connection:
            updated = connection.execute(
                """
                UPDATE bot_lesson_review_requests
                SET status = 'fulfilled', fulfilled_at = %s, updated_at = %s,
                    delivery_token = NULL, delivery_claimed_at = NULL,
                    delivery_lease_expires_at = NULL
                WHERE id = %s AND status = 'approved' AND delivery_token = %s
                  AND EXISTS (
                      SELECT 1 FROM bot_issued_lessons AS issued
                      WHERE issued.telegram_id = bot_lesson_review_requests.telegram_id
                        AND issued.lesson_number = bot_lesson_review_requests.lesson_number
                  )
                """,
                (now, now, request_id, delivery_token),
            )
        return updated.rowcount == 1
