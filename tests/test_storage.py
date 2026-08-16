import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from storage import (
    BotStorage,
    LessonAlreadyIssuedError,
    LessonDeliveryClaimStatus,
    LessonDeliveryInProgressError,
    LessonReviewStatus,
    MexcUidAlreadyBoundError,
    ReferralAssignment,
    StorageError,
    UserMexcUidConflictError,
)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.clock = 1_700_000_000_000
        self.storage = BotStorage(
            str(Path(self.temp_dir.name) / "state.sqlite3"),
            clock_ms=lambda: self.clock,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_inviter_is_set_once_and_cannot_be_replaced(self):
        self.storage.ensure_user(10)
        self.storage.ensure_user(20)

        self.assertEqual(
            self.storage.assign_inviter(30, 10),
            ReferralAssignment.ASSIGNED,
        )
        self.assertEqual(
            self.storage.assign_inviter(30, 20),
            ReferralAssignment.ALREADY_ASSIGNED,
        )
        self.assertEqual(self.storage.get_user(30).inviter_telegram_id, 10)

    def test_self_referral_and_unknown_inviter_are_rejected(self):
        self.assertEqual(
            self.storage.assign_inviter(10, 10),
            ReferralAssignment.SELF_REFERRAL,
        )
        self.assertEqual(
            self.storage.assign_inviter(20, 999),
            ReferralAssignment.INVITER_NOT_FOUND,
        )

    def test_mexc_uid_is_unique_and_user_cannot_replace_it(self):
        self.storage.bind_mexc_uid(10, "11111111")
        with self.assertRaises(MexcUidAlreadyBoundError):
            self.storage.bind_mexc_uid(20, "11111111")
        with self.assertRaises(UserMexcUidConflictError):
            self.storage.bind_mexc_uid(10, "22222222")

    def test_only_qualified_invited_users_are_counted_once(self):
        self.storage.ensure_user(10)
        self.storage.assign_inviter(20, 10)
        self.storage.assign_inviter(30, 10)
        self.storage.bind_mexc_uid(20, "11111111")
        self.storage.bind_mexc_uid(30, "22222222")
        self.storage.mark_qualified(20)
        self.storage.mark_qualified(20)

        self.assertEqual(self.storage.count_qualified_invites(10), 1)

    def test_activity_confirmation_is_persisted_only_once(self):
        self.storage.ensure_user(10)
        self.storage.record_activity_confirmation(
            10,
            confirmed_at=1000,
            baseline_last_trade_time=900,
        )
        self.storage.record_activity_confirmation(
            10,
            confirmed_at=2000,
            baseline_last_trade_time=1900,
        )

        state = self.storage.get_user(10)
        self.assertEqual(state.activity_confirmed_at, 1000)
        self.assertEqual(state.activity_baseline_last_trade_time, 900)

    def test_state_survives_storage_reinitialization(self):
        self.storage.ensure_user(10)
        self.storage.bind_mexc_uid(10, "11111111")
        self.storage.claim_lesson(10, 2)

        reopened = BotStorage(
            self.storage.database_path,
            clock_ms=lambda: self.clock + 1000,
        )

        self.assertEqual(reopened.get_user(10).mexc_uid, "11111111")
        self.assertTrue(reopened.is_lesson_issued(10, 2))

    def test_each_lesson_can_be_claimed_once_including_lesson1(self):
        for lesson_number in range(1, 8):
            with self.subTest(lesson_number=lesson_number):
                self.assertTrue(self.storage.claim_lesson(10, lesson_number))
                self.assertFalse(self.storage.claim_lesson(10, lesson_number))
        self.assertEqual(self.storage.issued_lessons(10), tuple(range(1, 8)))

    def test_lesson_delivery_parts_survive_released_lesson_claim(self):
        self.assertTrue(self.storage.claim_lesson(10, 3))
        self.assertTrue(self.storage.mark_lesson_part_delivered(10, 3, "main"))
        self.storage.release_lesson(10, 3)

        self.assertFalse(self.storage.is_lesson_issued(10, 3))
        self.assertTrue(self.storage.is_lesson_part_delivered(10, 3, "main"))
        self.assertFalse(self.storage.is_lesson_part_delivered(10, 3, "bonus"))
        self.assertTrue(self.storage.claim_lesson(10, 3))
        self.assertTrue(self.storage.mark_lesson_part_delivered(10, 3, "bonus"))
        self.assertFalse(self.storage.mark_lesson_part_delivered(10, 3, "bonus"))

        with self.assertRaises(ValueError):
            self.storage.is_lesson_part_delivered(10, 3, "invalid")

    def test_lesson_delivery_claim_is_leased_and_completed_after_send(self):
        first = self.storage.claim_lesson_delivery(
            10,
            3,
            "first-token",
            60_000,
        )
        busy = self.storage.claim_lesson_delivery(
            10,
            3,
            "second-token",
            60_000,
        )

        self.assertEqual(first, LessonDeliveryClaimStatus.ACQUIRED)
        self.assertEqual(busy, LessonDeliveryClaimStatus.BUSY)
        self.assertFalse(
            self.storage.complete_lesson_delivery(10, 3, "second-token")
        )

        self.clock += 60_001
        reclaimed = self.storage.claim_lesson_delivery(
            10,
            3,
            "second-token",
            60_000,
        )
        self.assertEqual(reclaimed, LessonDeliveryClaimStatus.ACQUIRED)
        self.assertTrue(
            self.storage.complete_lesson_delivery(10, 3, "second-token")
        )
        self.assertTrue(self.storage.is_lesson_issued(10, 3))
        self.assertEqual(
            self.storage.claim_lesson_delivery(10, 3, "third-token", 60_000),
            LessonDeliveryClaimStatus.ALREADY_ISSUED,
        )

    def test_failed_lesson_delivery_claim_can_be_released_immediately(self):
        self.assertEqual(
            self.storage.claim_lesson_delivery(10, 7, "first-token", 60_000),
            LessonDeliveryClaimStatus.ACQUIRED,
        )
        self.assertTrue(
            self.storage.release_lesson_delivery(10, 7, "first-token")
        )
        self.assertEqual(
            self.storage.claim_lesson_delivery(10, 7, "second-token", 60_000),
            LessonDeliveryClaimStatus.ACQUIRED,
        )

    def test_delivery_token_is_renewed_and_required_for_part_checkpoint(self):
        self.assertEqual(
            self.storage.claim_lesson_delivery(10, 3, "first-token", 60_000),
            LessonDeliveryClaimStatus.ACQUIRED,
        )
        self.assertFalse(
            self.storage.renew_lesson_delivery(10, 3, "wrong-token", 60_000)
        )
        self.assertTrue(
            self.storage.renew_lesson_delivery(10, 3, "first-token", 60_000)
        )
        self.assertFalse(
            self.storage.mark_claimed_lesson_part_delivered(
                10,
                3,
                "main",
                "wrong-token",
            )
        )
        self.assertTrue(
            self.storage.mark_claimed_lesson_part_delivered(
                10,
                3,
                "main",
                "first-token",
            )
        )

        self.clock += 60_001
        self.assertFalse(
            self.storage.renew_lesson_delivery(10, 3, "first-token", 60_000)
        )
        self.assertFalse(
            self.storage.mark_claimed_lesson_part_delivered(
                10,
                3,
                "bonus",
                "first-token",
            )
        )

    def test_active_delivery_blocks_review_creation_and_decision(self):
        self.storage.bind_mexc_uid(10, "11111111")
        self.assertEqual(
            self.storage.claim_lesson_delivery(10, 3, "first-token", 60_000),
            LessonDeliveryClaimStatus.ACQUIRED,
        )
        with self.assertRaises(LessonDeliveryInProgressError):
            self.storage.request_lesson_review(10, 3)

        self.storage.release_lesson_delivery(10, 3, "first-token")
        review = self.storage.request_lesson_review(10, 3).request
        self.assertEqual(
            self.storage.claim_lesson_delivery(10, 3, "second-token", 60_000),
            LessonDeliveryClaimStatus.ACQUIRED,
        )
        decision = self.storage.decide_lesson_review_request(
            review.request_id,
            LessonReviewStatus.REJECTED,
            7_629_218_005,
        )

        self.assertTrue(decision.delivery_in_progress)
        self.assertFalse(decision.changed)
        self.assertEqual(decision.request.status, LessonReviewStatus.PENDING)
        self.assertTrue(
            self.storage.complete_lesson_delivery(10, 3, "second-token")
        )
        self.assertEqual(
            self.storage.get_lesson_review_request(review.request_id).status,
            LessonReviewStatus.FULFILLED,
        )

    def test_rejection_notification_and_automatic_delivery_do_not_overlap(self):
        self.storage.bind_mexc_uid(10, "11111111")
        review = self.storage.request_lesson_review(10, 7).request
        decision = self.storage.decide_lesson_review_request(
            review.request_id,
            LessonReviewStatus.REJECTED,
            7_629_218_005,
            notification_token="notice-token",
            notification_lease_ms=60_000,
        )
        self.assertTrue(decision.notification_claimed)
        self.assertEqual(
            self.storage.claim_lesson_delivery(10, 7, "delivery-token", 60_000),
            LessonDeliveryClaimStatus.BUSY,
        )
        self.assertTrue(
            self.storage.complete_lesson_review_notification(
                review.request_id,
                "user",
                "notice-token",
            )
        )
        self.assertEqual(
            self.storage.claim_lesson_delivery(10, 7, "delivery-token", 60_000),
            LessonDeliveryClaimStatus.ACQUIRED,
        )

    def test_delivery_fulfills_active_review_without_rewriting_old_rejection(self):
        self.storage.bind_mexc_uid(10, "11111111")
        old_review = self.storage.request_lesson_review(10, 3).request
        self.storage.decide_lesson_review_request(
            old_review.request_id,
            LessonReviewStatus.REJECTED,
            7_629_218_005,
        )
        active_review = self.storage.request_lesson_review(10, 3).request
        self.storage.decide_lesson_review_request(
            active_review.request_id,
            LessonReviewStatus.APPROVED,
            7_629_218_005,
        )

        self.assertEqual(
            self.storage.claim_lesson_delivery(10, 3, "delivery-token", 60_000),
            LessonDeliveryClaimStatus.ACQUIRED,
        )
        self.assertTrue(
            self.storage.complete_lesson_delivery(10, 3, "delivery-token")
        )

        self.assertEqual(
            self.storage.get_lesson_review_request(old_review.request_id).status,
            LessonReviewStatus.REJECTED,
        )
        self.assertEqual(
            self.storage.get_lesson_review_request(active_review.request_id).status,
            LessonReviewStatus.FULFILLED,
        )

    def test_reject_decision_and_delivery_claim_are_serialized(self):
        self.storage.bind_mexc_uid(10, "11111111")
        review = self.storage.request_lesson_review(10, 7).request
        barrier = threading.Barrier(2)

        def reject():
            barrier.wait()
            return self.storage.decide_lesson_review_request(
                review.request_id,
                LessonReviewStatus.REJECTED,
                7_629_218_005,
                notification_token="notice-token",
                notification_lease_ms=60_000,
            )

        def claim_delivery():
            barrier.wait()
            return self.storage.claim_lesson_delivery(
                10,
                7,
                "delivery-token",
                60_000,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            reject_future = executor.submit(reject)
            delivery_future = executor.submit(claim_delivery)
            decision = reject_future.result()
            delivery_status = delivery_future.result()

        stored = self.storage.get_lesson_review_request(review.request_id)
        if decision.notification_claimed:
            self.assertEqual(delivery_status, LessonDeliveryClaimStatus.BUSY)
            self.assertEqual(stored.status, LessonReviewStatus.REJECTED)
        else:
            self.assertTrue(decision.delivery_in_progress)
            self.assertEqual(delivery_status, LessonDeliveryClaimStatus.ACQUIRED)
            self.assertEqual(stored.status, LessonReviewStatus.PENDING)

    def test_manual_review_accepts_only_lessons_3_and_7(self):
        self.storage.bind_mexc_uid(10, "11111111")

        for lesson_number in (3, 7):
            with self.subTest(lesson_number=lesson_number):
                submission = self.storage.request_lesson_review(10, lesson_number)
                self.assertTrue(submission.created)
                self.assertEqual(submission.request.lesson_number, lesson_number)

        for lesson_number in (1, 2, 4, 5, 6):
            with self.subTest(lesson_number=lesson_number):
                with self.assertRaises(ValueError):
                    self.storage.request_lesson_review(10, lesson_number)

    def test_manual_review_requires_bound_uid_and_is_idempotent(self):
        self.storage.ensure_user(10)
        with self.assertRaisesRegex(StorageError, "bound MEXC UID"):
            self.storage.request_lesson_review(10, 3)

        self.storage.bind_mexc_uid(10, "11111111")
        first = self.storage.request_lesson_review(10, 3)
        second = self.storage.request_lesson_review(10, 3)

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(second.request.request_id, first.request.request_id)
        self.assertEqual(second.request.mexc_uid, "11111111")
        self.assertEqual(second.request.status, LessonReviewStatus.PENDING)

    def test_manual_review_round_trips_64_bit_ids_and_notification(self):
        telegram_id = 7_629_218_005
        self.storage.bind_mexc_uid(telegram_id, "54458789")
        review = self.storage.request_lesson_review(telegram_id, 3).request
        self.assertTrue(self.storage.mark_lesson_review_notified(review.request_id))

        reopened = BotStorage(
            self.storage.database_path,
            clock_ms=lambda: self.clock + 1000,
        )
        stored = reopened.get_lesson_review_request(review.request_id)

        self.assertEqual(stored.telegram_id, telegram_id)
        self.assertEqual(stored.mexc_uid, "54458789")
        self.assertEqual(stored.admin_notified_at, self.clock)

    def test_manual_review_decision_is_immutable_and_approval_is_durable(self):
        admin_id = 7_629_218_005
        self.storage.bind_mexc_uid(10, "11111111")
        review = self.storage.request_lesson_review(10, 7).request

        approved = self.storage.decide_lesson_review_request(
            review.request_id,
            LessonReviewStatus.APPROVED,
            admin_id,
        )
        replay = self.storage.decide_lesson_review_request(
            review.request_id,
            LessonReviewStatus.APPROVED,
            admin_id,
        )
        conflict = self.storage.decide_lesson_review_request(
            review.request_id,
            LessonReviewStatus.REJECTED,
            admin_id,
        )
        duplicate = self.storage.request_lesson_review(10, 7)

        self.assertTrue(approved.changed)
        self.assertFalse(replay.changed)
        self.assertEqual(replay.request.status, LessonReviewStatus.APPROVED)
        self.assertFalse(conflict.changed)
        self.assertEqual(conflict.request.status, LessonReviewStatus.APPROVED)
        self.assertFalse(duplicate.created)
        self.assertEqual(duplicate.request.request_id, review.request_id)

        self.assertTrue(
            self.storage.claim_lesson_review_delivery(
                review.request_id,
                "delivery-token",
                60_000,
            )
        )
        self.assertTrue(self.storage.claim_lesson(10, 7))
        self.assertTrue(
            self.storage.mark_lesson_review_fulfilled(
                review.request_id,
                "delivery-token",
            )
        )
        fulfilled = self.storage.get_lesson_review_request(review.request_id)
        self.assertEqual(fulfilled.status, LessonReviewStatus.FULFILLED)
        self.assertEqual(fulfilled.decided_by, admin_id)
        self.assertEqual(fulfilled.fulfilled_at, self.clock)

        with self.assertRaises(LessonAlreadyIssuedError):
            self.storage.request_lesson_review(10, 7)

    def test_rejected_manual_review_allows_a_new_request(self):
        self.storage.bind_mexc_uid(10, "11111111")
        first = self.storage.request_lesson_review(10, 3).request
        rejected = self.storage.decide_lesson_review_request(
            first.request_id,
            LessonReviewStatus.REJECTED,
            7_629_218_005,
        )
        second = self.storage.request_lesson_review(10, 3)

        self.assertTrue(rejected.changed)
        self.assertEqual(rejected.request.status, LessonReviewStatus.REJECTED)
        self.assertTrue(second.created)
        self.assertNotEqual(second.request.request_id, first.request_id)

    def test_unknown_manual_review_decision_is_a_no_op(self):
        decision = self.storage.decide_lesson_review_request(
            999,
            LessonReviewStatus.APPROVED,
            7_629_218_005,
        )

        self.assertIsNone(decision.request)
        self.assertFalse(decision.changed)

    def test_manual_review_delivery_lease_has_one_owner_and_can_retry(self):
        self.storage.bind_mexc_uid(10, "11111111")
        review = self.storage.request_lesson_review(10, 3).request
        self.storage.decide_lesson_review_request(
            review.request_id,
            LessonReviewStatus.APPROVED,
            7_629_218_005,
        )

        self.assertTrue(
            self.storage.claim_lesson_review_delivery(
                review.request_id,
                "first-token",
                60_000,
            )
        )
        self.assertFalse(
            self.storage.claim_lesson_review_delivery(
                review.request_id,
                "second-token",
                60_000,
            )
        )
        self.assertFalse(
            self.storage.mark_lesson_review_fulfilled(
                review.request_id,
                "second-token",
            )
        )
        self.assertTrue(
            self.storage.release_lesson_review_delivery(
                review.request_id,
                "first-token",
            )
        )
        self.assertTrue(
            self.storage.claim_lesson_review_delivery(
                review.request_id,
                "second-token",
                60_000,
            )
        )

    def test_rejection_user_notification_is_recorded_once(self):
        self.storage.bind_mexc_uid(10, "11111111")
        review = self.storage.request_lesson_review(10, 7).request
        self.storage.decide_lesson_review_request(
            review.request_id,
            LessonReviewStatus.REJECTED,
            7_629_218_005,
        )

        self.assertTrue(
            self.storage.mark_lesson_review_user_notified(review.request_id)
        )
        first_timestamp = self.storage.get_lesson_review_request(
            review.request_id
        ).user_notified_at
        self.clock += 1000
        self.assertTrue(
            self.storage.mark_lesson_review_user_notified(review.request_id)
        )
        self.assertEqual(
            self.storage.get_lesson_review_request(review.request_id).user_notified_at,
            first_timestamp,
        )

    def test_review_notification_claim_has_one_owner_and_completes_once(self):
        self.storage.bind_mexc_uid(10, "11111111")
        review = self.storage.request_lesson_review(10, 3).request

        self.assertTrue(
            self.storage.claim_lesson_review_notification(
                review.request_id,
                "admin",
                "first-token",
                60_000,
            )
        )
        self.assertFalse(
            self.storage.claim_lesson_review_notification(
                review.request_id,
                "admin",
                "second-token",
                60_000,
            )
        )
        self.assertFalse(
            self.storage.complete_lesson_review_notification(
                review.request_id,
                "admin",
                "second-token",
            )
        )
        self.assertTrue(
            self.storage.complete_lesson_review_notification(
                review.request_id,
                "admin",
                "first-token",
            )
        )
        self.assertIsNotNone(
            self.storage.get_lesson_review_request(review.request_id).admin_notified_at
        )
        self.assertFalse(
            self.storage.claim_lesson_review_notification(
                review.request_id,
                "admin",
                "third-token",
                60_000,
            )
        )

    def test_review_notification_claim_can_be_released_or_reclaimed_after_lease(self):
        self.storage.bind_mexc_uid(10, "11111111")
        review = self.storage.request_lesson_review(10, 7).request
        self.storage.decide_lesson_review_request(
            review.request_id,
            LessonReviewStatus.REJECTED,
            7_629_218_005,
        )

        self.assertTrue(
            self.storage.claim_lesson_review_notification(
                review.request_id,
                "user",
                "first-token",
                60_000,
            )
        )
        self.assertFalse(
            self.storage.release_lesson_review_notification(
                review.request_id,
                "user",
                "wrong-token",
            )
        )
        self.assertTrue(
            self.storage.release_lesson_review_notification(
                review.request_id,
                "user",
                "first-token",
            )
        )
        self.assertTrue(
            self.storage.claim_lesson_review_notification(
                review.request_id,
                "user",
                "second-token",
                60_000,
            )
        )
        self.clock += 60_001
        self.assertTrue(
            self.storage.claim_lesson_review_notification(
                review.request_id,
                "user",
                "third-token",
                60_000,
            )
        )
        self.assertTrue(
            self.storage.complete_lesson_review_notification(
                review.request_id,
                "user",
                "third-token",
            )
        )
        self.assertIsNotNone(
            self.storage.get_lesson_review_request(review.request_id).user_notified_at
        )

    def test_concurrent_review_notification_claims_have_one_winner(self):
        self.storage.bind_mexc_uid(10, "11111111")
        review = self.storage.request_lesson_review(10, 3).request
        barrier = threading.Barrier(2)

        def claim(token):
            barrier.wait()
            return self.storage.claim_lesson_review_notification(
                review.request_id,
                "admin",
                token,
                60_000,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = list(executor.map(claim, ("first-token", "second-token")))

        self.assertEqual(sum(claims), 1)

    def test_concurrent_manual_submissions_create_one_request(self):
        self.storage.bind_mexc_uid(10, "11111111")
        barrier = threading.Barrier(2)

        def submit():
            barrier.wait()
            return self.storage.request_lesson_review(10, 3)

        with ThreadPoolExecutor(max_workers=2) as executor:
            submissions = list(executor.map(lambda _: submit(), range(2)))

        self.assertEqual(sum(item.created for item in submissions), 1)
        self.assertEqual(
            len({item.request.request_id for item in submissions}),
            1,
        )

    def test_concurrent_opposite_decisions_have_one_winner(self):
        self.storage.bind_mexc_uid(10, "11111111")
        review = self.storage.request_lesson_review(10, 7).request
        barrier = threading.Barrier(2)

        def decide(status):
            barrier.wait()
            return self.storage.decide_lesson_review_request(
                review.request_id,
                status,
                7_629_218_005,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            decisions = list(
                executor.map(
                    decide,
                    (LessonReviewStatus.APPROVED, LessonReviewStatus.REJECTED),
                )
            )

        self.assertEqual(sum(item.changed for item in decisions), 1)
        terminal_statuses = {item.request.status for item in decisions}
        self.assertEqual(len(terminal_statuses), 1)
        self.assertIn(
            terminal_statuses.pop(),
            (LessonReviewStatus.APPROVED, LessonReviewStatus.REJECTED),
        )


if __name__ == "__main__":
    unittest.main()
