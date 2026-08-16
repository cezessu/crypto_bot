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


class LessonAlreadyIssuedError(StorageError):
    """A manual review cannot be opened for an already issued lesson."""


class LessonDeliveryInProgressError(StorageError):
    """A manual review cannot start while the same lesson is being delivered."""


class ReferralAssignment(str, Enum):
    ASSIGNED = "assigned"
    ALREADY_ASSIGNED = "already_assigned"
    SELF_REFERRAL = "self_referral"
    INVITER_NOT_FOUND = "inviter_not_found"


class LessonReviewStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    FULFILLED = "fulfilled"


class LessonDeliveryClaimStatus(str, Enum):
    ACQUIRED = "acquired"
    ALREADY_ISSUED = "already_issued"
    BUSY = "busy"


@dataclass(frozen=True)
class UserState:
    telegram_id: int
    mexc_uid: Optional[str]
    inviter_telegram_id: Optional[int]
    activity_confirmed_at: Optional[int]
    activity_baseline_last_trade_time: Optional[int]
    qualified_at: Optional[int]


@dataclass(frozen=True)
class LessonReviewRequest:
    request_id: int
    telegram_id: int
    lesson_number: int
    mexc_uid: str
    status: LessonReviewStatus
    requested_at: int
    updated_at: int
    admin_notified_at: Optional[int]
    user_notified_at: Optional[int]
    decided_at: Optional[int]
    decided_by: Optional[int]
    fulfilled_at: Optional[int]


@dataclass(frozen=True)
class LessonReviewSubmission:
    request: LessonReviewRequest
    created: bool


@dataclass(frozen=True)
class LessonReviewDecision:
    request: Optional[LessonReviewRequest]
    changed: bool
    delivery_in_progress: bool = False
    notification_claimed: bool = False
    lesson_already_issued: bool = False


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

                CREATE TABLE IF NOT EXISTS lesson_delivery_claims (
                    telegram_id INTEGER NOT NULL,
                    lesson_number INTEGER NOT NULL CHECK (
                        lesson_number BETWEEN 1 AND 7
                    ),
                    delivery_token TEXT NOT NULL,
                    claimed_at INTEGER NOT NULL,
                    lease_expires_at INTEGER NOT NULL,
                    PRIMARY KEY (telegram_id, lesson_number),
                    FOREIGN KEY (telegram_id)
                        REFERENCES users(telegram_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS lesson_delivery_parts (
                    telegram_id INTEGER NOT NULL,
                    lesson_number INTEGER NOT NULL CHECK (
                        lesson_number BETWEEN 1 AND 7
                    ),
                    part TEXT NOT NULL CHECK (part IN ('main', 'bonus')),
                    delivered_at INTEGER NOT NULL,
                    PRIMARY KEY (telegram_id, lesson_number, part),
                    FOREIGN KEY (telegram_id)
                        REFERENCES users(telegram_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS lesson_review_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL,
                    lesson_number INTEGER NOT NULL CHECK (
                        lesson_number IN (3, 7)
                    ),
                    mexc_uid TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN (
                            'pending', 'approved', 'rejected', 'fulfilled'
                        )
                    ),
                    requested_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    admin_notified_at INTEGER,
                    user_notified_at INTEGER,
                    decided_at INTEGER,
                    decided_by INTEGER,
                    fulfilled_at INTEGER,
                    delivery_token TEXT,
                    delivery_claimed_at INTEGER,
                    delivery_lease_expires_at INTEGER,
                    FOREIGN KEY (telegram_id)
                        REFERENCES users(telegram_id)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_lesson_review_requests_user
                    ON lesson_review_requests(telegram_id);

                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_lesson_review_requests_active
                    ON lesson_review_requests(telegram_id, lesson_number)
                    WHERE status IN ('pending', 'approved');

                CREATE TABLE IF NOT EXISTS lesson_review_notification_claims (
                    request_id INTEGER NOT NULL,
                    recipient TEXT NOT NULL CHECK (recipient IN ('admin', 'user')),
                    notification_token TEXT NOT NULL,
                    claimed_at INTEGER NOT NULL,
                    lease_expires_at INTEGER NOT NULL,
                    PRIMARY KEY (request_id, recipient),
                    FOREIGN KEY (request_id)
                        REFERENCES lesson_review_requests(id)
                        ON DELETE CASCADE
                );
                """
            )

    @staticmethod
    def _lesson_review_from_row(row: sqlite3.Row) -> LessonReviewRequest:
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
            connection.execute("BEGIN IMMEDIATE")
            issued = connection.execute(
                """
                SELECT 1 FROM issued_lessons
                WHERE telegram_id = ? AND lesson_number = ?
                """,
                (telegram_id, lesson_number),
            ).fetchone()
            if issued is not None:
                return LessonDeliveryClaimStatus.ALREADY_ISSUED

            rejection_notice = connection.execute(
                """
                SELECT 1
                FROM lesson_review_requests AS review
                JOIN lesson_review_notification_claims AS notification
                  ON notification.request_id = review.id
                WHERE review.telegram_id = ? AND review.lesson_number = ?
                  AND review.status = 'rejected'
                  AND notification.recipient = 'user'
                  AND notification.lease_expires_at > ?
                """,
                (telegram_id, lesson_number, now),
            ).fetchone()
            if rejection_notice is not None:
                return LessonDeliveryClaimStatus.BUSY

            current = connection.execute(
                """
                SELECT lease_expires_at FROM lesson_delivery_claims
                WHERE telegram_id = ? AND lesson_number = ?
                """,
                (telegram_id, lesson_number),
            ).fetchone()
            if current is not None and current["lease_expires_at"] > now:
                return LessonDeliveryClaimStatus.BUSY

            connection.execute(
                """
                INSERT INTO lesson_delivery_claims (
                    telegram_id, lesson_number, delivery_token,
                    claimed_at, lease_expires_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(telegram_id, lesson_number) DO UPDATE SET
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
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE lesson_delivery_claims
                SET claimed_at = ?, lease_expires_at = ?
                WHERE telegram_id = ? AND lesson_number = ?
                  AND delivery_token = ? AND lease_expires_at > ?
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
            connection.execute("BEGIN IMMEDIATE")
            claim = connection.execute(
                """
                SELECT 1 FROM lesson_delivery_claims
                WHERE telegram_id = ? AND lesson_number = ?
                  AND delivery_token = ? AND lease_expires_at > ?
                """,
                (telegram_id, lesson_number, delivery_token, now),
            ).fetchone()
            if claim is None:
                return False
            connection.execute(
                """
                INSERT INTO issued_lessons (telegram_id, lesson_number, issued_at)
                VALUES (?, ?, ?)
                ON CONFLICT(telegram_id, lesson_number) DO NOTHING
                """,
                (telegram_id, lesson_number, now),
            )
            connection.execute(
                """
                UPDATE lesson_review_requests
                SET status = 'fulfilled', fulfilled_at = ?, updated_at = ?,
                    delivery_token = NULL, delivery_claimed_at = NULL,
                    delivery_lease_expires_at = NULL
                WHERE telegram_id = ? AND lesson_number = ?
                  AND status IN ('pending', 'approved')
                """,
                (now, now, telegram_id, lesson_number),
            )
            connection.execute(
                """
                DELETE FROM lesson_delivery_claims
                WHERE telegram_id = ? AND lesson_number = ?
                  AND delivery_token = ?
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
                DELETE FROM lesson_delivery_claims
                WHERE telegram_id = ? AND lesson_number = ?
                  AND delivery_token = ?
                """,
                (telegram_id, lesson_number, delivery_token),
            )
        return deleted.rowcount == 1

    def release_lesson(self, telegram_id: int, lesson_number: int) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                DELETE FROM issued_lessons
                WHERE telegram_id = ? AND lesson_number = ?
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
                SELECT 1 FROM lesson_delivery_parts
                WHERE telegram_id = ? AND lesson_number = ? AND part = ?
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
                INSERT INTO lesson_delivery_parts (
                    telegram_id, lesson_number, part, delivered_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_id, lesson_number, part) DO NOTHING
                """,
                (telegram_id, lesson_number, part, self.clock_ms()),
            )
        return inserted.rowcount == 1

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
            connection.execute("BEGIN IMMEDIATE")
            claim = connection.execute(
                """
                SELECT 1 FROM lesson_delivery_claims
                WHERE telegram_id = ? AND lesson_number = ?
                  AND delivery_token = ? AND lease_expires_at > ?
                """,
                (telegram_id, lesson_number, delivery_token, now),
            ).fetchone()
            if claim is None:
                return False
            inserted = connection.execute(
                """
                INSERT INTO lesson_delivery_parts (
                    telegram_id, lesson_number, part, delivered_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_id, lesson_number, part) DO NOTHING
                """,
                (telegram_id, lesson_number, part, now),
            )
        return inserted.rowcount == 1

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

    def is_lesson_delivery_in_progress(
        self,
        telegram_id: int,
        lesson_number: int,
    ) -> bool:
        now = self.clock_ms()
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM lesson_delivery_claims
                WHERE telegram_id = ? AND lesson_number = ?
                  AND lease_expires_at > ?
                """,
                (telegram_id, lesson_number, now),
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
            connection.execute("BEGIN IMMEDIATE")
            user = connection.execute(
                "SELECT mexc_uid FROM users WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()
            if user is None or user["mexc_uid"] is None:
                raise StorageError("Manual review requires a bound MEXC UID")

            issued = connection.execute(
                """
                SELECT 1 FROM issued_lessons
                WHERE telegram_id = ? AND lesson_number = ?
                """,
                (telegram_id, lesson_number),
            ).fetchone()
            if issued is not None:
                raise LessonAlreadyIssuedError("Lesson has already been issued")

            delivery = connection.execute(
                """
                SELECT 1 FROM lesson_delivery_claims
                WHERE telegram_id = ? AND lesson_number = ?
                  AND lease_expires_at > ?
                """,
                (telegram_id, lesson_number, now),
            ).fetchone()
            if delivery is not None:
                raise LessonDeliveryInProgressError(
                    "Lesson delivery is already in progress"
                )

            row = connection.execute(
                """
                SELECT id, telegram_id, lesson_number, mexc_uid, status,
                       requested_at, updated_at, admin_notified_at, user_notified_at,
                       decided_at, decided_by, fulfilled_at
                FROM lesson_review_requests
                WHERE telegram_id = ? AND lesson_number = ?
                  AND status IN ('pending', 'approved')
                """,
                (telegram_id, lesson_number),
            ).fetchone()
            if row is not None:
                return LessonReviewSubmission(
                    request=self._lesson_review_from_row(row),
                    created=False,
                )

            inserted = connection.execute(
                """
                INSERT INTO lesson_review_requests (
                    telegram_id, lesson_number, mexc_uid, status,
                    requested_at, updated_at
                ) VALUES (?, ?, ?, 'pending', ?, ?)
                """,
                (telegram_id, lesson_number, user["mexc_uid"], now, now),
            )
            row = connection.execute(
                """
                SELECT id, telegram_id, lesson_number, mexc_uid, status,
                       requested_at, updated_at, admin_notified_at, user_notified_at,
                       decided_at, decided_by, fulfilled_at
                FROM lesson_review_requests
                WHERE id = ?
                """,
                (inserted.lastrowid,),
            ).fetchone()
        return LessonReviewSubmission(
            request=self._lesson_review_from_row(row),
            created=True,
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
                FROM lesson_review_requests
                WHERE id = ?
                """,
                (request_id,),
            ).fetchone()
        return None if row is None else self._lesson_review_from_row(row)

    def mark_lesson_review_notified(self, request_id: int) -> bool:
        now = self.clock_ms()
        with self._connection() as connection:
            updated = connection.execute(
                """
                UPDATE lesson_review_requests
                SET admin_notified_at = COALESCE(admin_notified_at, ?),
                    updated_at = ?
                WHERE id = ? AND status IN ('pending', 'approved')
                """,
                (now, now, request_id),
            )
        return updated.rowcount == 1

    def mark_lesson_review_user_notified(self, request_id: int) -> bool:
        now = self.clock_ms()
        with self._connection() as connection:
            updated = connection.execute(
                """
                UPDATE lesson_review_requests
                SET user_notified_at = COALESCE(user_notified_at, ?),
                    updated_at = ?
                WHERE id = ? AND status = 'rejected'
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
            connection.execute("BEGIN IMMEDIATE")
            review = connection.execute(
                """
                SELECT status, admin_notified_at, user_notified_at
                FROM lesson_review_requests
                WHERE id = ?
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
                    SELECT 1 FROM lesson_delivery_claims
                    WHERE telegram_id = (
                        SELECT telegram_id FROM lesson_review_requests WHERE id = ?
                    )
                      AND lesson_number = (
                        SELECT lesson_number FROM lesson_review_requests WHERE id = ?
                    )
                      AND lease_expires_at > ?
                    """,
                    (request_id, request_id, now),
                ).fetchone()
                if delivery is not None:
                    return False

            claim = connection.execute(
                """
                SELECT lease_expires_at
                FROM lesson_review_notification_claims
                WHERE request_id = ? AND recipient = ?
                """,
                (request_id, recipient),
            ).fetchone()
            if claim is None:
                connection.execute(
                    """
                    INSERT INTO lesson_review_notification_claims (
                        request_id, recipient, notification_token,
                        claimed_at, lease_expires_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        request_id,
                        recipient,
                        notification_token,
                        now,
                        now + lease_ms,
                    ),
                )
                return True
            if claim["lease_expires_at"] > now:
                return False

            updated = connection.execute(
                """
                UPDATE lesson_review_notification_claims
                SET notification_token = ?, claimed_at = ?, lease_expires_at = ?
                WHERE request_id = ? AND recipient = ? AND lease_expires_at <= ?
                """,
                (
                    notification_token,
                    now,
                    now + lease_ms,
                    request_id,
                    recipient,
                    now,
                ),
            )
        return updated.rowcount == 1

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
            connection.execute("BEGIN IMMEDIATE")
            claim = connection.execute(
                """
                SELECT notification_token
                FROM lesson_review_notification_claims
                WHERE request_id = ? AND recipient = ?
                """,
                (request_id, recipient),
            ).fetchone()
            if claim is None or claim["notification_token"] != notification_token:
                return False
            updated = connection.execute(
                f"""
                UPDATE lesson_review_requests
                SET {notified_column} = COALESCE({notified_column}, ?),
                    updated_at = ?
                WHERE id = ? AND {status_clause}
                """,
                (now, now, request_id),
            )
            connection.execute(
                """
                DELETE FROM lesson_review_notification_claims
                WHERE request_id = ? AND recipient = ? AND notification_token = ?
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
                DELETE FROM lesson_review_notification_claims
                WHERE request_id = ? AND recipient = ? AND notification_token = ?
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
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT id, telegram_id, lesson_number, mexc_uid, status,
                       requested_at, updated_at, admin_notified_at, user_notified_at,
                       decided_at, decided_by, fulfilled_at
                FROM lesson_review_requests
                WHERE id = ?
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
                    SELECT 1 FROM issued_lessons
                    WHERE telegram_id = ? AND lesson_number = ?
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
                    connection.execute(
                        """
                        UPDATE lesson_review_requests
                        SET status = 'fulfilled', fulfilled_at = ?, updated_at = ?
                        WHERE id = ? AND status IN ('pending', 'approved')
                        """,
                        (now, now, request_id),
                    )
                    row = connection.execute(
                        """
                        SELECT id, telegram_id, lesson_number, mexc_uid, status,
                               requested_at, updated_at, admin_notified_at,
                               user_notified_at, decided_at, decided_by, fulfilled_at
                        FROM lesson_review_requests
                        WHERE id = ?
                        """,
                        (request_id,),
                    ).fetchone()
                    return LessonReviewDecision(
                        request=self._lesson_review_from_row(row),
                        changed=False,
                    )

                delivery = connection.execute(
                    """
                    SELECT 1 FROM lesson_delivery_claims
                    WHERE telegram_id = ? AND lesson_number = ?
                      AND lease_expires_at > ?
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
                connection.execute(
                    """
                    UPDATE lesson_review_requests
                    SET status = ?, decided_by = ?, decided_at = ?, updated_at = ?
                    WHERE id = ? AND status = 'pending'
                    """,
                    (status.value, reviewer_telegram_id, now, now, request_id),
                )
            row = connection.execute(
                """
                SELECT id, telegram_id, lesson_number, mexc_uid, status,
                       requested_at, updated_at, admin_notified_at, user_notified_at,
                       decided_at, decided_by, fulfilled_at
                FROM lesson_review_requests
                WHERE id = ?
                """,
                (request_id,),
            ).fetchone()
            notification_claimed = False
            if (
                notification_token is not None
                and row["status"] == LessonReviewStatus.REJECTED.value
                and row["user_notified_at"] is None
            ):
                claim = connection.execute(
                    """
                    SELECT lease_expires_at
                    FROM lesson_review_notification_claims
                    WHERE request_id = ? AND recipient = 'user'
                    """,
                    (request_id,),
                ).fetchone()
                if claim is None:
                    connection.execute(
                        """
                        INSERT INTO lesson_review_notification_claims (
                            request_id, recipient, notification_token,
                            claimed_at, lease_expires_at
                        ) VALUES (?, 'user', ?, ?, ?)
                        """,
                        (
                            request_id,
                            notification_token,
                            now,
                            now + notification_lease_ms,
                        ),
                    )
                    notification_claimed = True
                elif claim["lease_expires_at"] <= now:
                    updated = connection.execute(
                        """
                        UPDATE lesson_review_notification_claims
                        SET notification_token = ?, claimed_at = ?,
                            lease_expires_at = ?
                        WHERE request_id = ? AND recipient = 'user'
                          AND lease_expires_at <= ?
                        """,
                        (
                            notification_token,
                            now,
                            now + notification_lease_ms,
                            request_id,
                            now,
                        ),
                    )
                    notification_claimed = updated.rowcount == 1
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
                UPDATE lesson_review_requests
                SET delivery_token = ?, delivery_claimed_at = ?,
                    delivery_lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND status = 'approved'
                  AND (
                      delivery_token IS NULL
                      OR delivery_lease_expires_at <= ?
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
                UPDATE lesson_review_requests
                SET delivery_token = NULL, delivery_claimed_at = NULL,
                    delivery_lease_expires_at = NULL, updated_at = ?
                WHERE id = ? AND status = 'approved' AND delivery_token = ?
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
                UPDATE lesson_review_requests
                SET status = 'fulfilled', fulfilled_at = ?, updated_at = ?,
                    delivery_token = NULL, delivery_claimed_at = NULL,
                    delivery_lease_expires_at = NULL
                WHERE id = ? AND status = 'approved' AND delivery_token = ?
                  AND EXISTS (
                      SELECT 1 FROM issued_lessons AS issued
                      WHERE issued.telegram_id = lesson_review_requests.telegram_id
                        AND issued.lesson_number = lesson_review_requests.lesson_number
                  )
                """,
                (now, now, request_id, delivery_token),
            )
        return updated.rowcount == 1


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
