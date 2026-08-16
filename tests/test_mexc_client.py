import hashlib
import hmac
import unittest
from urllib.parse import parse_qs, urlencode, urlparse

try:
    from _requests_stub import install_requests_stub_if_missing
except ModuleNotFoundError:
    from tests._requests_stub import install_requests_stub_if_missing

install_requests_stub_if_missing()

import requests

from mexc_client import (
    REBATE_DETAIL_ENDPOINT,
    REBATE_HISTORY_ENDPOINT,
    MexcAPIError,
    MexcClient,
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
    def __init__(self, responses=None, exception=None):
        if responses is None:
            self.responses = []
        elif isinstance(responses, list):
            self.responses = list(responses)
        else:
            self.responses = [responses]
        self.exception = exception
        self.calls = []

    def get(self, url, headers, timeout):
        self.calls.append({"url": url, "headers": headers, "timeout": timeout})
        if self.exception:
            raise self.exception
        if not self.responses:
            raise AssertionError(f"Unexpected HTTP request: {url}")
        return self.responses.pop(0)


def page_payload(records, *, page=1, total_pages=1):
    return {
        "page": page,
        "totalRecords": len(records),
        "totalPageNum": total_pages,
        "data": records,
    }


def history_record(uid="12345678", invite_time=1637651320000):
    return {
        "spot": "0.01",
        "futures": "0",
        "total": "0.01",
        "uid": uid,
        "account": "user@example.com",
        "inviteTime": invite_time,
    }


def detail_record(uid="12345678", trade_time=1700000000000):
    return {
        "asset": "USDT",
        "type": "spot",
        "rate": "0.3",
        "amount": "0.001",
        "uid": uid,
        "account": "user@example.com",
        "tradeTime": trade_time,
        "updateTime": trade_time,
    }


class MexcClientTests(unittest.TestCase):
    def make_client(self, *responses, exception=None):
        session = FakeSession(list(responses), exception=exception)
        client = MexcClient(
            "test-key",
            "test-secret",
            session=session,
            clock_ms=lambda: 1700000000123,
        )
        return client, session

    def test_signature_is_built_from_exact_query_string(self):
        client, _ = self.make_client()
        params = [("page", 1), ("note", "a b")]
        signed_query = client.build_signed_query(params, timestamp_ms=1700000000123)

        unsigned_query = urlencode(
            params + [("recvWindow", 5000), ("timestamp", 1700000000123)]
        )
        expected_signature = hmac.new(
            b"test-secret",
            unsigned_query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        self.assertEqual(signed_query, f"{unsigned_query}&signature={expected_signature}")

    def test_standard_rebate_responses_return_referral_and_trade(self):
        client, session = self.make_client(
            FakeResponse(payload=page_payload([history_record()])),
            FakeResponse(
                payload=page_payload(
                    [
                        detail_record(trade_time=1700000000),
                        detail_record(trade_time=1700000100000),
                    ]
                )
            ),
        )

        referral = client.get_rebate_referral("12345678")

        self.assertIsNotNone(referral)
        self.assertEqual(referral.uid, "12345678")
        self.assertIsNone(referral.deposit_amount)
        self.assertIsNone(referral.trading_amount)
        self.assertEqual(referral.invite_time, 1637651320000)
        self.assertEqual(referral.first_trade_time, 1700000000000)
        self.assertEqual(referral.last_trade_time, 1700000100000)
        self.assertEqual(len(session.calls), 2)
        self.assertIn(REBATE_HISTORY_ENDPOINT, session.calls[0]["url"])
        self.assertIn(REBATE_DETAIL_ENDPOINT, session.calls[1]["url"])
        for call in session.calls:
            self.assertEqual(call["headers"], {"X-MEXC-APIKEY": "test-key"})
            query = parse_qs(urlparse(call["url"]).query)
            self.assertEqual(query["page"], ["1"])
            self.assertEqual(query["recvWindow"], ["5000"])
            self.assertEqual(query["timestamp"], ["1700000000123"])
            self.assertIn("signature", query)

    def test_uid_not_found_returns_none_without_requesting_trade_details(self):
        client, session = self.make_client(
            FakeResponse(payload=page_payload([], total_pages=0))
        )

        self.assertIsNone(client.get_rebate_referral("12345678"))
        self.assertEqual(len(session.calls), 1)

    def test_different_uid_is_not_accepted(self):
        client, session = self.make_client(
            FakeResponse(payload=page_payload([history_record(uid="11111111")]))
        )

        self.assertIsNone(client.get_rebate_referral("12345678"))
        self.assertEqual(len(session.calls), 1)

    def test_referral_without_rebate_trade_is_returned_without_trade_time(self):
        client, _ = self.make_client(
            FakeResponse(payload=page_payload([history_record()])),
            FakeResponse(payload=page_payload([], total_pages=0)),
        )

        referral = client.get_rebate_referral("12345678")

        self.assertIsNotNone(referral)
        self.assertIsNone(referral.first_trade_time)
        self.assertIsNone(referral.last_trade_time)

    def test_history_and_detail_are_paginated_and_filtered_locally(self):
        client, session = self.make_client(
            FakeResponse(
                payload=page_payload(
                    [history_record(uid="11111111")], page=1, total_pages=2
                )
            ),
            FakeResponse(
                payload=page_payload([history_record()], page=2, total_pages=2)
            ),
            FakeResponse(
                payload=page_payload(
                    [detail_record(uid="11111111")], page=1, total_pages=2
                )
            ),
            FakeResponse(
                payload=page_payload(
                    [detail_record(trade_time=1700000200000)], page=2, total_pages=2
                )
            ),
        )

        referral = client.get_rebate_referral("12345678")

        self.assertEqual(referral.first_trade_time, 1700000200000)
        pages = [parse_qs(urlparse(call["url"]).query)["page"][0] for call in session.calls]
        self.assertEqual(pages, ["1", "2", "1", "2"])

    def test_missing_invite_time_does_not_hide_a_matching_uid(self):
        record = history_record()
        record.pop("inviteTime")
        client, _ = self.make_client(
            FakeResponse(payload=page_payload([record])),
            FakeResponse(payload=page_payload([detail_record()])),
        )

        referral = client.get_rebate_referral("12345678")

        self.assertIsNotNone(referral)
        self.assertIsNone(referral.invite_time)

    def test_timeout_is_mapped_to_safe_error(self):
        client, _ = self.make_client(exception=requests.Timeout("secret URL"))
        with self.assertRaises(MexcTimeoutError):
            client.get_rebate_referral("12345678")

    def test_rate_limit_is_mapped(self):
        client, _ = self.make_client(FakeResponse(status_code=429))
        with self.assertRaises(MexcRateLimitError):
            client.get_rebate_referral("12345678")

    def test_server_error_is_mapped(self):
        client, _ = self.make_client(FakeResponse(status_code=503))
        with self.assertRaises(MexcServiceError):
            client.get_rebate_referral("12345678")

    def test_generic_4xx_is_mapped(self):
        client, _ = self.make_client(FakeResponse(status_code=400, payload={"code": 0}))
        with self.assertRaises(MexcHTTPError):
            client.get_rebate_referral("12345678")

    def test_mexc_api_error_code_is_mapped(self):
        client, _ = self.make_client(
            FakeResponse(status_code=400, payload={"code": 700002})
        )
        with self.assertRaises(MexcAPIError) as context:
            client.get_rebate_referral("12345678")
        self.assertEqual(context.exception.api_code, 700002)

    def test_error_code_in_http_200_is_not_accepted_as_success(self):
        client, _ = self.make_client(
            FakeResponse(status_code=200, payload={"code": 601, "msg": "denied"})
        )
        with self.assertRaises(MexcAPIError) as context:
            client.get_rebate_referral("12345678")
        self.assertEqual(context.exception.api_code, 601)

    def test_invalid_json_is_mapped(self):
        client, _ = self.make_client(
            FakeResponse(status_code=200, json_error=ValueError("bad json"))
        )
        with self.assertRaises(MexcInvalidResponseError):
            client.get_rebate_referral("12345678")

    def test_invalid_rebate_shape_is_mapped(self):
        client, _ = self.make_client(
            FakeResponse(payload={"page": 1, "totalPageNum": 1, "data": {}})
        )
        with self.assertRaises(MexcInvalidResponseError):
            client.get_rebate_referral("12345678")

    def test_absent_environment_variables_disable_client(self):
        self.assertIsNone(MexcClient.from_env({}))
        self.assertIsNone(MexcClient.from_env({"MEXC_API_KEY": "only-key"}))

    def test_copy_paste_whitespace_in_environment_credentials_is_removed(self):
        client = MexcClient.from_env(
            {
                "MEXC_API_KEY": "  test-\nkey ",
                "MEXC_API_SECRET": " test-secret\n",
            }
        )
        self.assertIsNotNone(client)
        self.assertEqual(client.api_key, "test-key")
        self.assertEqual(client.api_secret, "test-secret")

    def test_uid_must_contain_digits_only(self):
        client, _ = self.make_client()
        with self.assertRaises(ValueError):
            client.get_rebate_referral("1234abc")


if __name__ == "__main__":
    unittest.main()
