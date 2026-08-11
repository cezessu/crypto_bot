import unittest

from referrals import build_referral_link, parse_referral_payload


class ReferralLinkTests(unittest.TestCase):
    def test_build_referral_link(self):
        self.assertEqual(
            build_referral_link("@example_bot", 12345),
            "https://t.me/example_bot?start=ref_12345",
        )

    def test_parse_valid_payload(self):
        self.assertEqual(parse_referral_payload("/start ref_12345"), 12345)

    def test_reject_invalid_payloads(self):
        for value in (None, "/start", "/start ref_0", "/start ref_me", "/help ref_1"):
            with self.subTest(value=value):
                self.assertIsNone(parse_referral_payload(value))


if __name__ == "__main__":
    unittest.main()
