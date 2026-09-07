"""Read-only Bitunix Partnership API client, isolated from the production bot.

Uses the partner-supplied guide, updated 2026-09-01. Signature ordering follows
its Python/JavaScript examples (character class, then sum of key ASCII codes).
Credentials and raw response bodies must never be logged.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request

class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

BASE_URL = "https://partners.bitunix.com"
READ_METHODS = {
    "/partner/api/v1/openapi/userList": "GET",
    "/partner/api/v2/openapi/validateUser": "POST",
    "/partner/api/v1/openapi/transAmountList": "GET",
    "/partner/api/v1/openapi/transaction": "GET",
    "/partner/api/v1/openapi/totalTransaction": "GET",
    "/partner/api/v1/openapi/dailyTransAmountList": "POST",
    "/partner/api/v2/openapi/queryPnlAndTradeCnt": "GET",
}


class PartnerAPIError(Exception):
    def __init__(self, code, *, http_status=None):
        self.code = str(code) if str(code).isascii() and str(code).isdigit() else "unclassified"
        self.http_status = http_status
        super().__init__(f"Partner API error code={self.code}, HTTP={http_status}")


def sorted_parameter_names(params):
    def order(name):
        if not name or not name.isascii():
            raise ValueError("Parameter names must be nonempty ASCII")
        kind = 1 if name[0].isdigit() else 2 if name[0].islower() else 3
        return kind, sum(map(ord, name))
    return sorted(params, key=order)


def sign_parameters(params, secret):
    values = "".join(str(params[name]) for name in sorted_parameter_names(params) if params[name] is not None)
    return hashlib.sha1((values + secret).encode("utf-8")).hexdigest()


class PartnerClient:
    def __init__(self, key, secret, *, clock=time.time, opener=None):
        if not key or not secret or not (key + secret).isascii() or any(c.isspace() for c in key + secret):
            raise ValueError("Invalid credential format")
        self._key, self._secret = key, secret
        self._clock = clock
        self._opener = opener or urllib.request.build_opener(NoRedirects())

    def request(self, endpoint, params=None):
        if endpoint not in READ_METHODS:
            raise ValueError("Endpoint is not an approved read operation")
        params = {k: v for k, v in (params or {}).items() if v is not None}
        params["timestamp"] = int(self._clock())
        ordered = {name: params[name] for name in sorted_parameter_names(params)}
        headers = {"apiKey": self._key, "signature": sign_parameters(ordered, self._secret)}
        method = READ_METHODS[endpoint]
        encoded = urllib.parse.urlencode(ordered)
        url, body = BASE_URL + endpoint, None
        if method == "GET":
            url += "?" + encoded
        else:
            body = encoded.encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded;charset=UTF-8"
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            try:
                response = self._opener.open(req, timeout=15)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                status = response.code
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise PartnerAPIError("oversized_response", http_status=status)
            data = json.loads(raw)
        except PartnerAPIError:
            raise
        except (OSError, ValueError):
            raise PartnerAPIError("transport_or_json_error") from None
        if not isinstance(data, dict) or status != 200 or str(data.get("code")) != "0" or data.get("success") is False:
            raise PartnerAPIError(data.get("code") if isinstance(data, dict) else None, http_status=status)
        if "result" not in data:
            raise PartnerAPIError("missing_result", http_status=status)
        return data["result"]

    def user_list(self, *, uid=None, page=1, page_size=1):
        return self.request("/partner/api/v1/openapi/userList", {"uid": uid, "page": page, "pageSize": page_size})

    def validate_user(self, uid):
        return self.request("/partner/api/v2/openapi/validateUser", {"account": str(uid)})

