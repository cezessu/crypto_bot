import unittest
from decimal import Decimal

from _requests_stub import install_requests_stub_if_missing

install_requests_stub_if_missing()

from eligibility import (
    ACTIVITY_PERIOD_MS,
    ActivityState,
    EligibilityStatus,
    evaluate_lesson,
    lesson_requires_referral_data,
)
from mexc_client import ReferralData


def referral(deposit="0", volume="0", first_trade=None, last_trade=None):
    return ReferralData(
        uid="12345678",
        deposit_amount=Decimal(deposit),
        trading_amount=Decimal(volume),
        first_trade_time=first_trade,
        last_trade_time=last_trade,
    )


class EligibilityTests(unittest.TestCase):
    def test_lesson2_is_eligible(self):
        result = evaluate_lesson(2, referral(deposit="100", first_trade=1))
        self.assertEqual(result.status, EligibilityStatus.ELIGIBLE)

    def test_lesson2_requires_deposit_and_first_trade(self):
        cases = (
            referral(deposit="99.999", first_trade=1),
            referral(deposit="100", first_trade=None),
            referral(deposit="0", first_trade=None),
        )
        for value in cases:
            with self.subTest(value=value):
                self.assertFalse(evaluate_lesson(2, value).is_eligible)

    def test_lesson3_volume(self):
        self.assertTrue(evaluate_lesson(3, referral(volume="300")).is_eligible)
        self.assertFalse(evaluate_lesson(3, referral(volume="299.999")).is_eligible)

    def test_lesson4_qualified_invite_count(self):
        self.assertFalse(evaluate_lesson(4, qualified_invites=0).is_eligible)
        self.assertTrue(evaluate_lesson(4, qualified_invites=1).is_eligible)
        self.assertFalse(lesson_requires_referral_data(4))

    def test_lesson5_waits_thirty_days(self):
        confirmed = 1_700_000_000_000
        state = ActivityState(confirmed_at_ms=confirmed)
        result = evaluate_lesson(
            5,
            referral(last_trade=confirmed),
            activity_state=state,
            now_ms=confirmed + ACTIVITY_PERIOD_MS - 1,
        )
        self.assertFalse(result.is_eligible)

    def test_lesson5_requires_trade_on_or_after_control_date(self):
        confirmed = 1_700_000_000_000
        due = confirmed + ACTIVITY_PERIOD_MS
        state = ActivityState(confirmed_at_ms=confirmed)

        stale = evaluate_lesson(
            5,
            referral(last_trade=due - 1),
            activity_state=state,
            now_ms=due,
        )
        active = evaluate_lesson(
            5,
            referral(last_trade=due),
            activity_state=state,
            now_ms=due,
        )

        self.assertFalse(stale.is_eligible)
        self.assertTrue(active.is_eligible)

    def test_lesson5_without_confirmation_is_not_eligible(self):
        result = evaluate_lesson(5, now_ms=1_700_000_000_000)
        self.assertFalse(result.is_eligible)

    def test_lesson6_qualified_invite_count(self):
        self.assertFalse(evaluate_lesson(6, qualified_invites=1).is_eligible)
        self.assertTrue(evaluate_lesson(6, qualified_invites=2).is_eligible)

    def test_lesson7_volume_or_three_invites(self):
        self.assertTrue(evaluate_lesson(7, referral(volume="5000")).is_eligible)
        self.assertFalse(
            evaluate_lesson(7, referral(volume="4999.999"), qualified_invites=2).is_eligible
        )
        self.assertTrue(evaluate_lesson(7, qualified_invites=3).is_eligible)
        self.assertFalse(lesson_requires_referral_data(7, qualified_invites=3))


if __name__ == "__main__":
    unittest.main()
