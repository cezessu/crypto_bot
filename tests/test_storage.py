import tempfile
import unittest
from pathlib import Path

from storage import (
    BotStorage,
    MexcUidAlreadyBoundError,
    ReferralAssignment,
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


if __name__ == "__main__":
    unittest.main()
