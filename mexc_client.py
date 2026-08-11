"""Minimal public MEXC Affiliate API client used by the Telegram bot."""

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
AFFILIATE_REFERRAL_ENDPOINT = "/api/v3/rebate/affiliate/referral"
AFFILIATE_HISTORY_START_MS = 1609459200000  # 2021-01-01 UTC


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
        700002: "⚠️ MEXC отклонил подпись запроса. Администратору нужно проверить API-ключ.",
        700003: "⚠️ Время запроса не совпало со временем MEXC. Попробуйте позже.",
        700006: "⚠️ MEXC отклонил исходящий IP-адрес сервиса.",
        700007: "⚠️ API-ключ MEXC не имеет доступа к Affiliate endpoint.",
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
    """Only the public Affiliate fields required by current lesson rules."""

    uid: str
    deposit_amount: Decimal
    trading_amount: Decimal
    first_trade_time: Optional[int]


class MexcClient:
    """Signed client for the documented public MEXC Affiliate API."""

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
        """Create a client from Render variables, or return None when absent."""

        source = os.environ if environ is None else environ
        api_key = source.get("MEXC_API_KEY", "")
        api_secret = source.get("MEXC_API_SECRET", "")
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
        """Build and sign the exact query string that will be sent."""

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

        if 400 <= status_code:
            api_code = payload.get("code")
            if api_code not in (None, 0, "0"):
                logger.warning(
                    "MEXC API rejected request endpoint=%s status=%s code=%s",
                    endpoint,
                    status_code,
                    api_code,
                )
                raise MexcAPIError(api_code)
            logger.warning("MEXC HTTP error endpoint=%s status=%s", endpoint, status_code)
            raise MexcHTTPError(f"MEXC HTTP status {status_code}")

        api_code = payload.get("code")
        if api_code not in (0, "0"):
            logger.warning("MEXC API error endpoint=%s code=%s", endpoint, api_code)
            raise MexcAPIError(api_code)

        logger.info("MEXC request completed endpoint=%s status=%s", endpoint, status_code)
        return payload

    def get_affiliate_referral(self, uid: str) -> Optional[ReferralData]:
        """Return a direct affiliate referral by UID, or None when it is absent."""

        timestamp_ms = self.clock_ms()
        payload = self._request(
            AFFILIATE_REFERRAL_ENDPOINT,
            (
                ("uid", uid),
                ("startTime", AFFILIATE_HISTORY_START_MS),
                ("endTime", timestamp_ms),
                ("page", 1),
                ("pageSize", 10),
            ),
            timestamp_ms=timestamp_ms,
        )

        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise MexcInvalidResponseError("MEXC response has no data object")
        result_list = data.get("resultList")
        if not isinstance(result_list, list):
            raise MexcInvalidResponseError("MEXC response has no resultList")

        referral = next(
            (
                item
                for item in result_list
                if isinstance(item, Mapping) and str(item.get("uid", "")) == uid
            ),
            None,
        )
        if referral is None:
            return None

        return ReferralData(
            uid=uid,
            deposit_amount=self._parse_decimal(referral.get("depositAmount"), "depositAmount"),
            trading_amount=self._parse_decimal(referral.get("tradingAmount"), "tradingAmount"),
            first_trade_time=self._parse_optional_timestamp(referral.get("firstTradeTime")),
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
    def _parse_optional_timestamp(value: object) -> Optional[int]:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise MexcInvalidResponseError("MEXC field firstTradeTime is invalid") from exc
