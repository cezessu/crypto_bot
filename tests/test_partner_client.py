import hashlib
import json
import unittest
from urllib.parse import parse_qs, urlsplit

from partner_client import PartnerAPIError, PartnerClient, sign_parameters, sorted_parameter_names
from partner_client import NoRedirects


class Response:
    def __init__(self, payload, status=200):
        self.code = status
        self.raw = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit):
        return self.raw[:limit]


class Opener:
    def __init__(self, payload, status=200):
        self.response = Response(payload, status)
        self.requests = []

    def open(self, request, **kwargs):
        self.requests.append(request)
        return self.response


class PartnerTests(unittest.TestCase):
    def test_published_signature_example(self):
        params = {"name": "bitunix", "age": 18, "Country": "USA", "24hProfit": "0.59", "timestamp": 1694173446}
        expected = hashlib.sha1(b"0.5918bitunix1694173446USAthisistestapisecret").hexdigest()
        self.assertEqual(sign_parameters(params, "thisistestapisecret"), expected)

    def test_sort_uses_ascii_sum_not_alphabetical_order(self):
        self.assertEqual(sorted_parameter_names({"aa": 1, "b": 2, "A": 3, "1n": 4}), ["1n", "b", "aa", "A"])

    def test_get_matches_guide_auth_and_seconds(self):
        opener = Opener({"code": "0", "result": {"items": [], "total": 0}})
        client = PartnerClient("dummy-key", "dummy-secret", clock=lambda: 1694173446.9, opener=opener)
        client.user_list(page_size=10)
        req = opener.requests[0]
        params = parse_qs(urlsplit(req.full_url).query)
        self.assertEqual(params, {"page": ["1"], "pageSize": ["10"], "timestamp": ["1694173446"]})
        headers = {k.lower(): v for k, v in req.header_items()}
        self.assertEqual(headers["apikey"], "dummy-key")
        self.assertEqual(headers["signature"], hashlib.sha1(b"1101694173446dummy-secret").hexdigest())
        self.assertNotIn("dummy-key", req.full_url)
        self.assertNotIn("dummy-secret", req.full_url)

    def test_validate_user_is_form_post(self):
        opener = Opener({"code": "0", "result": {"result": True}})
        client = PartnerClient("dummy-key", "dummy-secret", clock=lambda: 1000, opener=opener)
        self.assertEqual(client.validate_user("123456"), {"result": True})
        req = opener.requests[0]
        self.assertEqual(req.method, "POST")
        self.assertEqual(parse_qs(req.data.decode()), {"account": ["123456"], "timestamp": ["1000"]})
        self.assertTrue(req.get_header("Content-type").startswith("application/x-www-form-urlencoded"))

    def test_false_referral_is_not_transport_failure(self):
        client = PartnerClient("dummy-key", "dummy-secret", opener=Opener({"code": "0", "result": False}))
        self.assertIs(client.user_list(uid="123456"), False)

    def test_http200_with_api_error_is_not_success_and_message_not_logged(self):
        client = PartnerClient("dummy-key", "dummy-secret", opener=Opener({"code": "100004", "msg": "dummy-secret"}))
        with self.assertRaises(PartnerAPIError) as raised:
            client.user_list()
        self.assertEqual(raised.exception.code, "100004")
        self.assertNotIn("dummy-secret", str(raised.exception))

    def test_missing_result_rejected(self):
        client = PartnerClient("dummy-key", "dummy-secret", opener=Opener({"code": "0"}))
        with self.assertRaises(PartnerAPIError):
            client.user_list()

    def test_http_failure_with_zero_api_code_rejected(self):
        client = PartnerClient("dummy-key", "dummy-secret", opener=Opener({"code": "0", "result": {}}, status=503))
        with self.assertRaises(PartnerAPIError):
            client.user_list()

    def test_unapproved_endpoint_is_never_sent(self):
        opener = Opener({"code": "0", "result": {}})
        client = PartnerClient("dummy-key", "dummy-secret", opener=opener)
        with self.assertRaises(ValueError):
            client.request("/partners-admin/admin/private/v1/partner/user/generatorApiKey")
        self.assertEqual(opener.requests, [])

    def test_redirect_never_forwards_credentials(self):
        self.assertIsNone(NoRedirects().redirect_request(None, None, 302, "Found", {}, "https://example.com"))


if __name__ == "__main__":
    unittest.main()
