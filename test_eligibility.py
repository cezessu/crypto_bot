import unittest
from decimal import Decimal

from eligibility import (
    EligibilityStatus,
    evaluate_condition,
    evaluate_lesson,
    lesson_requires_referral_data,
)
from mexc_client import ReferralData


def referral(deposit="0", volume="0", first_trade=None):
    return ReferralData(
        uid="12345678",
        deposit_amount=Decimal(deposit),
        trading_amount=Decimal(volume),
        first_trade_time=first_trade,
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
                result = evaluate_lesson(2, value)
                self.assertEqual(result.status, EligibilityStatus.INELIGIBLE)

    def test_lesson3_volume_is_eligible(self):
        result = evaluate_lesson(3, referral(volume="300"))
        self.assertEqual(result.status, EligibilityStatus.ELIGIBLE)

    def test_lesson3_volume_is_not_eligible(self):
        result = evaluate_lesson(3, referral(volume="299.999"))
        self.assertEqual(result.status, EligibilityStatus.INELIGIBLE)

    def test_lesson7_volume_is_eligible(self):
        result = evaluate_lesson(7, referral(volume="5000"))
        self.assertEqual(result.status, EligibilityStatus.ELIGIBLE)

    def test_lesson7_volume_is_not_eligible(self):
        result = evaluate_lesson(7, referral(volume="4999.999"))
        self.assertEqual(result.status, EligibilityStatus.INELIGIBLE)
        self.assertIn("приглашённым друзьям", result.message)

    def test_lessons_4_5_6_are_explicitly_unverifiable(self):
        for lesson_number in (4, 5, 6):
            with self.subTest(lesson_number=lesson_number):
                result = evaluate_lesson(lesson_number)
                self.assertEqual(result.status, EligibilityStatus.UNVERIFIABLE)
                self.assertFalse(lesson_requires_referral_data(lesson_number))

    def test_lesson7_friends_branch_is_separate_and_unverifiable(self):
        result = evaluate_condition("lesson_7_qualified_friends", None)
        self.assertEqual(result.status, EligibilityStatus.UNVERIFIABLE)


if __name__ == "__main__":
    unittest.main()
