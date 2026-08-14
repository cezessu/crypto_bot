"""Signed client for the standard MEXC Spot rebate API.

The bot deliberately uses endpoints available to a regular read-only API key.
Affiliate-only data such as a referral's deposit and aggregate trade volume is
not fabricated from rebate amounts: those fields are represented as ``None``.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Callable, Iterable, Optional, Tuple
from urllib.parse import urlencode

import requests


logger = logging.getLogger(__name__)

MEXC_BASE_URL = "https://api.mexc.com"
REBATE_HISTORY_ENDPOINT = "/api/v3/rebate/taxQuery"
REBATE_DETAIL_ENDPOINT = "/api/v3/rebate/detail"
MAX_REBATE_PAGES = 500


class MexcClientError(Exception):
    """Base exception with a safe message that can be shown to a bot user."""

    public_message = "⚠️ MEXC временно не смог выполнить проверку. Попробуйте позже."
    kind = "mexc_error"


class MexcConfigurationError(MexcClientError):
    public_message = "⚠️ Проверка MEXC временно недоступна. Обратитесь к администратору."
    kind = "configuration"


class MexcTimeoutError(MexcClientError):
    public_message = "⏳ MEXC не ответил вовремя. Попробуйте проверить условие ещё раз позже."
    kind = "timeout"


class MexcRateLimitError(MexcClientError):
    public_message = "⏳ MEXC временно ограничил частоту запросов. Попробуйте через несколько минут."
    kind = "rate_limit"


class MexcHTTPError(MexcClientError):
    kind = "http_error"


class MexcServiceError(MexcClientError):
    public_message = "⚠️ Сервис MEXC временно недоступен. Попробуйте позже."
    kind = "service_error"


class MexcInvalidResponseError(MexcClientError):
    public_message = "⚠️ MEXC вернул некорректный ответ. Попробуйте позже."
    kind = "invalid_response"


class MexcAPIError(MexcClientError):
    kind = "api_error"

    _PUBLIC_MESSAGES = {
        401: "⚠️ MEXC отклонил доступ API-ключа. Администратору нужно проверить права ключа.",
        602: "⚠️ MEXC отклонил подпись запроса. Администратору нужно проверить API-ключ.",
        10072: "⚠️ MEXC не принял API-ключ. Администратору нужно проверить ключ.",
        700001: "⚠️ MEXC не принял формат API-ключа. Администратору нужно проверить ключ.",
        700002: "⚠️ MEXC отклонил подпись запроса. Администратору нужно проверить API-ключ.",
        700003: "⚠️ Время запроса не совпало со временем MEXC. Попробуйте позже.",
        700006: "⚠️ MEXC отклонил исходящий IP-адрес сервиса.",
        700007: "⚠️ API-ключ MEXC не имеет доступа к этому методу.",
    }

    def __init__(self, api_code: object):
        self.api_code = api_code
        try:
            normalized_code = int(api_code)
        except (TypeError, ValueError):
            normalized_code = None
        self.public_message = self._PUBLIC_MESSAGES.get(
            normalized_code,
            "⚠️ MEXC не смог выполнить проверку. Попробуйте позже или обратитесь к администратору.",
        )
        super().__init__(f"MEXC API error code={normalized_code!r}")


@dataclass(frozen=True)
class ReferralData:
    """Referral data available to a regular MEXC API key."""

    uid: str
    deposit_amount: Optional[Decimal]
    trading_amount: Optional[Decimal]
    first_trade_time: Optional[int]
    last_trade_time: Optional[int]
    invite_time: Optional[int] = None


class MexcClient:
    """Signed client for documented read-only MEXC rebate endpoints."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        base_url: str = MEXC_BASE_URL,
        recv_window_ms: int = 5000,
        timeout: Tuple[float, float] = (3.05, 10.0),
        session: Optional[requests.Session] = None,
        clock_ms: Optional[Callable[[], int]] = None,
    ) -> None:
        self._validate_credentials(api_key, api_secret)
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.recv_window_ms = recv_window_ms
        self.timeout = timeout
        self.session = session or requests.Session()
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    @classmethod
    def from_env(
        cls,
        environ: Optional[Mapping[str, str]] = None,
        **kwargs: object,
    ) -> Optional["MexcClient"]:
        source = os.environ if environ is None else environ
        api_key = "".join(source.get("MEXC_API_KEY", "").split())
        api_secret = "".join(source.get("MEXC_API_SECRET", "").split())
        if not api_key or not api_secret:
            return None
        return cls(api_key, api_secret, **kwargs)

    @staticmethod
    def _validate_credentials(api_key: str, api_secret: str) -> None:
        if not api_key or not api_secret:
            raise MexcConfigurationError("MEXC credentials are missing")
        if any(character.isspace() for character in api_key) or any(
            character.isspace() for character in api_secret
        ):
            raise MexcConfigurationError("MEXC credentials contain whitespace")

    def build_signed_query(
        self,
        params: Iterable[Tuple[str, object]],
        *,
        timestamp_ms: Optional[int] = None,
    ) -> str:
        timestamp = self.clock_ms() if timestamp_ms is None else timestamp_ms
        ordered_params = list(params)
        ordered_params.append(("recvWindow", self.recv_window_ms))
        ordered_params.append(("timestamp", timestamp))
        query_string = urlencode(ordered_params)
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"{query_string}&signature={signature}"

    def _request(
        self,
        endpoint: str,
        params: Iterable[Tuple[str, object]],
        *,
        timestamp_ms: Optional[int] = None,
    ) -> MutableMapping[str, object]:
        signed_query = self.build_signed_query(params, timestamp_ms=timestamp_ms)
        url = f"{self.base_url}{endpoint}?{signed_query}"
        headers = {"X-MEXC-APIKEY": self.api_key}

        logger.info("MEXC request started endpoint=%s", endpoint)
        try:
            response = self.session.get(url, headers=headers, timeout=self.timeout)
        except requests.Timeout as exc:
            logger.warning("MEXC request timed out endpoint=%s", endpoint)
            raise MexcTimeoutError("MEXC request timed out") from exc
        except requests.RequestException as exc:
            logger.warning("MEXC network request failed endpoint=%s", endpoint)
            raise MexcServiceError("MEXC network request failed") from exc

        status_code = response.status_code
        if status_code == 429:
            logger.warning("MEXC rate limit endpoint=%s status=%s", endpoint, status_code)
            raise MexcRateLimitError("MEXC rate limit")
        if 500 <= status_code:
            logger.warning("MEXC service error endpoint=%s status=%s", endpoint, status_code)
            raise MexcServiceError(f"MEXC HTTP status {status_code}")

        try:
            payload = response.json()
        except (ValueError, TypeError) as exc:
            logger.warning("MEXC returned invalid JSON endpoint=%s status=%s", endpoint, status_code)
            raise MexcInvalidResponseError("MEXC returned invalid JSON") from exc

        if not isinstance(payload, MutableMapping):
            raise MexcInvalidResponseError("MEXC response root is not an object")

        api_code = payload.get("code")
        has_api_code = "code" in payload
        if 400 <= status_code:
            if has_api_code and api_code not in (None, 0, "0"):
                logger.warning(
                    "MEXC API rejected request endpoint=%s status=%s code=%s",
                    endpoint,
                    status_code,
                    api_code,
                )
                raise MexcAPIError(api_code)
            logger.warning("MEXC HTTP error endpoint=%s status=%s", endpoint, status_code)
            raise MexcHTTPError(f"MEXC HTTP status {status_code}")

        if payload.get("success") is False or (
            has_api_code and api_code not in (None, 0, "0")
        ):
            logger.warning("MEXC API error endpoint=%s code=%s", endpoint, api_code)
            raise MexcAPIError(api_code)

        logger.info("MEXC request completed endpoint=%s status=%s", endpoint, status_code)
        return payload

    def _get_rebate_page(
        self,
        endpoint: str,
        page: int,
    ) -> tuple[list[Mapping[str, object]], int]:
        payload = self._request(endpoint, (("page", page),))
        data = payload.get("data")
        if not isinstance(data, list):
            raise MexcInvalidResponseError("MEXC response has no rebate data list")

        try:
            total_pages = int(payload.get("totalPageNum", 1))
        except (TypeError, ValueError) as exc:
            raise MexcInvalidResponseError("MEXC totalPageNum is invalid") from exc
        # Some API implementations use zero pages for an empty result set.
        # Treat that as one empty terminal page instead of a malformed answer.
        if total_pages == 0 and not data:
            total_pages = 1
        if total_pages < 1 or total_pages > MAX_REBATE_PAGES:
            raise MexcInvalidResponseError("MEXC totalPageNum is outside the safe limit")

        records = [record for record in data if isinstance(record, Mapping)]
        if len(records) != len(data):
            raise MexcInvalidResponseError("MEXC rebate record is invalid")
        return records, total_pages

    def _iter_rebate_records(self, endpoint: str) -> Iterable[Mapping[str, object]]:
        page = 1
        while True:
            records, total_pages = self._get_rebate_page(endpoint, page)
            yield from records
            if page >= total_pages:
                return
            page += 1

    def get_rebate_referral(self, uid: str) -> Optional[ReferralData]:
        """Find a referred UID and rebate-generating trades visible to this key."""

        normalized_uid = str(uid).strip()
        if not normalized_uid.isdigit():
            raise ValueError("uid must contain digits only")

        referral_found = False
        invite_time: Optional[int] = None
        for record in self._iter_rebate_records(REBATE_HISTORY_ENDPOINT):
            if str(record.get("uid", "")) == normalized_uid:
                referral_found = True
                invite_time = self._parse_optional_timestamp(
                    record.get("inviteTime"), "inviteTime"
                )
                break

        if not referral_found:
            return None

        trade_times = []
        for record in self._iter_rebate_records(REBATE_DETAIL_ENDPOINT):
            if str(record.get("uid", "")) == normalized_uid:
                trade_time = self._parse_optional_timestamp(
                    record.get("tradeTime"), "tradeTime"
                )
                if trade_time is not None:
                    trade_times.append(trade_time)

        return ReferralData(
            uid=normalized_uid,
            deposit_amount=None,
            trading_amount=None,
            first_trade_time=min(trade_times) if trade_times else None,
            last_trade_time=max(trade_times) if trade_times else None,
            invite_time=invite_time,
        )

    @staticmethod
    def _parse_decimal(value: object, field_name: str) -> Decimal:
        if value is None or isinstance(value, bool):
            raise MexcInvalidResponseError(f"MEXC field {field_name} is missing")
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise MexcInvalidResponseError(f"MEXC field {field_name} is invalid") from exc

    @staticmethod
    def _parse_optional_timestamp(value: object, field_name: str) -> Optional[int]:
        if value in (None, ""):
            return None
        try:
            timestamp = int(value)
        except (TypeError, ValueError) as exc:
            raise MexcInvalidResponseError(f"MEXC field {field_name} is invalid") from exc
        if timestamp < 0:
            raise MexcInvalidResponseError(f"MEXC field {field_name} is invalid")
        if timestamp == 0:
            return None
        if 0 < timestamp < 100_000_000_000:
            timestamp *= 1000
        return timestamp
