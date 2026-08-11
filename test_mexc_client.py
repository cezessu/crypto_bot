import hashlib
import hmac
import unittest
from decimal import Decimal
from urllib.parse import urlencode

import requests

from mexc_client import (
    MexcAPIError,
    MexcClient,
    MexcConfigurationError,
    MexcHTTPError,
    MexcInvalidResponseError,
    MexcRateLimitError,
    MexcServiceError,
    MexcTimeoutError,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, json_error=None):
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise self._json_error
        return self._payload


class FakeSession:
    def __init__(self, response=None, exception=None):
        self.response = response
        self.exception = exception
        self.calls = []

    def get(self, url, headers, timeout):
        self.calls.append({"url": url, "headers": headers, "timeout": timeout})
        if self.exception:
            raise self.exception
        return self.response


def referral_payload(uid="12345678", deposit="150.25", volume="5123.75", first_trade=123456789):
    return {
        "success": True,
        "code": 0,
        "message": None,
        "data": {
            "pageSize": 10,
            "totalCount": 1,
            "totalPage": 1,
            "currentPage": 1,
            "resultList": [
                {
                    "uid": uid,
                    "depositAmount": deposit,
                    "tradingAmount": volume,
                    "firstTradeTime": first_trade,
                }
            ],
        },
    }


class MexcClientTests(unittest.TestCase):
    def make_client(self, response=None, exception=None):
        session = FakeSession(response=response, exception=exception)
        client = MexcClient(
            "test-key",
            "test-secret",
            session=session,
            clock_ms=lambda: 1700000000123,
        )
        return client, session

    def test_signature_is_built_from_exact_query_string(self):
        client, _ = self.make_client()
        params = [("uid", "12345678"), ("note", "a b")]
        signed_query = client.build_signed_query(params, timestamp_ms=1700000000123)

        unsigned_query = urlencode(
            params + [("recvWindow", 5000), ("timestamp", 1700000000123)]
        )
        expected_signature = hmac.new(
            b"test-secret",
            unsigned_query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        self.assertEqual(
            signed_query,
            f"{unsigned_query}&signature={expected_signature}",
        )

    def test_code_zero_returns_referral_and_uses_required_header(self):
        client, session = self.make_client(FakeResponse(payload=referral_payload()))

        referral = client.get_affiliate_referral("12345678")

        self.assertIsNotNone(referral)
        self.assertEqual(referral.uid, "12345678")
        self.assertEqual(referral.deposit_amount, Decimal("150.25"))
        self.assertEqual(referral.trading_amount, Decimal("5123.75"))
        self.assertEqual(referral.first_trade_time, 123456789)
        self.assertEqual(session.calls[0]["headers"], {"X-MEXC-APIKEY": "test-key"})
        self.assertIn("?uid=12345678&startTime=", session.calls[0]["url"])
        self.assertIn("&signature=", session.calls[0]["url"])

    def test_uid_not_found_returns_none(self):
        payload = referral_payload()
        payload["data"]["resultList"] = []
        client, _ = self.make_client(FakeResponse(payload=payload))

        self.assertIsNone(client.get_affiliate_referral("12345678"))

    def test_different_uid_is_not_accepted(self):
        client, _ = self.make_client(
            FakeResponse(payload=referral_payload(uid="11111111"))
        )

        self.assertIsNone(client.get_affiliate_referral("12345678"))

    def test_timeout_is_mapped_to_safe_error(self):
        client, _ = self.make_client(exception=requests.Timeout("secret URL"))

        with self.assertRaises(MexcTimeoutError):
            client.get_affiliate_referral("12345678")

    def test_rate_limit_is_mapped(self):
        client, _ = self.make_client(FakeResponse(status_code=429))

        with self.assertRaises(MexcRateLimitError):
            client.get_affiliate_referral("12345678")

    def test_server_error_is_mapped(self):
        client, _ = self.make_client(FakeResponse(status_code=503))

        with self.assertRaises(MexcServiceError):
            client.get_affiliate_referral("12345678")

    def test_generic_4xx_is_mapped(self):
        client, _ = self.make_client(
            FakeResponse(status_code=400, payload={"code": 0})
        )

        with self.assertRaises(MexcHTTPError):
            client.get_affiliate_referral("12345678")

    def test_mexc_api_error_code_is_mapped(self):
        client, _ = self.make_client(
            FakeResponse(status_code=400, payload={"code": 700002})
        )

        with self.assertRaises(MexcAPIError) as context:
            client.get_affiliate_referral("12345678")
        self.assertEqual(context.exception.api_code, 700002)

    def test_invalid_json_is_mapped(self):
        client, _ = self.make_client(
            FakeResponse(status_code=200, json_error=ValueError("bad json"))
        )

        with self.assertRaises(MexcInvalidResponseError):
            client.get_affiliate_referral("12345678")

    def test_absent_environment_variables_disable_client(self):
        self.assertIsNone(MexcClient.from_env({}))
        self.assertIsNone(MexcClient.from_env({"MEXC_API_KEY": "only-key"}))

    def test_whitespace_in_environment_secret_is_rejected(self):
        with self.assertRaises(MexcConfigurationError):
            MexcClient.from_env(
                {"MEXC_API_KEY": "test-key", "MEXC_API_SECRET": "secret with-space"}
            )


if __name__ == "__main__":
    unittest.main()
