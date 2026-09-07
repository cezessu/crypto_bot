"""Normalize documented partner data without guessing missing volume or history."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import threading
import time

from mexc_client import MexcClientError, ReferralData
from partner_client import PartnerClient, PartnerAPIError

PREFIX = "/partner/api/v1/openapi/"


class BitunixError(MexcClientError):
    kind = "bitunix_check_failed"
    public_message = "⚠️ Bitunix не дал полный ответ для проверки. Урок пока не выдан. Повторите позже или обратитесь к администратору."


def utc_date(value):
    if not isinstance(value, str):
        raise BitunixError()
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError()
        return result.astimezone(timezone.utc)
    except ValueError:
        raise BitunixError() from None


def amount(value):
    if value is None or isinstance(value, bool):
        raise BitunixError()
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0:
            raise InvalidOperation()
        return result
    except InvalidOperation:
        raise BitunixError() from None


class BitunixClient:
    def __init__(self, key=None, secret=None, *, partner=None, now=None, interval=0.4):
        self.partner = partner or PartnerClient(key, secret)
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.interval = interval
        self.lock = threading.Lock()
        self.last_request = 0

    def _request(self, endpoint, params):
        with self.lock:
            delay = self.interval - (time.monotonic() - self.last_request)
            if delay > 0:
                time.sleep(delay)
            self.last_request = time.monotonic()
            try:
                return self.partner.request(endpoint, params)
            except PartnerAPIError:
                raise BitunixError() from None

    def _pages(self, endpoint, params):
        total = None
        count = 0
        fingerprints = set()
        for page in range(1, 101):
            response = self._request(PREFIX + endpoint, {**params, "page": page, "pageSize": 100})
            if not isinstance(response, dict) or not isinstance(response.get("items"), list):
                raise BitunixError()
            actual_total = response.get("total")
            if isinstance(actual_total, bool) or not isinstance(actual_total, int) or actual_total < 0:
                raise BitunixError()
            if total is None:
                total = actual_total
            if total != actual_total:
                raise BitunixError()  # Changing pages cannot form a complete snapshot.
            items = response["items"]
            if len(items) > 100 or count + len(items) > total:
                raise BitunixError()
            fingerprint = json.dumps(items, sort_keys=True)
            if items and fingerprint in fingerprints:
                raise BitunixError()
            fingerprints.add(fingerprint)
            count += len(items)
            for item in items:
                if not isinstance(item, dict):
                    raise BitunixError()
                yield item
            if count == total:
                return
            if not items:
                raise BitunixError()
        raise BitunixError()  # Never award from truncated history.

    def get_rebate_referral(self, uid):
        uid = str(uid)
        if not uid.isascii() or not uid.isdigit() or len(uid) > 32:
            raise BitunixError()
        direct = self._request("/partner/api/v2/openapi/validateUser", {"account": uid})
        if not isinstance(direct, dict) or type(direct.get("result")) is not bool:
            raise BitunixError()
        if not direct["result"]:
            return None
        member = self._request(PREFIX + "userList", {"uid": uid})
        if member is False:
            return None
        if member is not True:
            raise BitunixError()
        registration = None
        for row in self._pages("userList", {}):
            if str(row.get("uid")) == uid:
                registration = utc_date(row.get("registerTime"))
                break
        end = self.now().astimezone(timezone.utc).replace(microsecond=0)
        if registration is None or registration > end:
            raise BitunixError()
        start = registration.replace(microsecond=0)
        volume = Decimal(0)
        volume_known = True
        first = last = None
        windows = 0
        while start <= end:
            windows += 1
            if windows > 30:
                raise BitunixError()
            stop = min(start + timedelta(days=179), end)
            params = {"uid": uid, "startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "endTime": stop.strftime("%Y-%m-%dT%H:%M:%SZ")}
            for row in self._pages("transAmountList", params):
                if str(row.get("uid")) != uid:
                    raise BitunixError()
                if row.get("transVolumeUsd") is None:
                    volume_known = False  # Guide's old example omits this field.
                else:
                    volume += amount(row["transVolumeUsd"])
            for row in self._pages("transaction", {**params, "userType": "agent"}):
                if str(row.get("uid")) != uid:
                    raise BitunixError()
                traded = amount(row.get("tradeAmount"))
                when = utc_date(row.get("ctime"))
                if not start <= when < stop + timedelta(seconds=1):
                    raise BitunixError()
                if traded > 0:
                    ts = int(when.timestamp() * 1000)
                    first = ts if first is None else min(first, ts)
                    last = ts if last is None else max(last, ts)
            start = stop + timedelta(seconds=1)
        return ReferralData(uid=uid, deposit_amount=None,
                            trading_amount=volume if volume_known else None,
                            first_trade_time=first, last_trade_time=last,
                            invite_time=int(registration.timestamp() * 1000))

