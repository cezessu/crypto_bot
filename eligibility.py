"""Eligibility rules based on persisted Telegram state and public MEXC fields."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Optional

from mexc_client import ReferralData


DAY_MS = 24 * 60 * 60 * 1000
ACTIVITY_PERIOD_MS = 30 * DAY_MS


class EligibilityStatus(str, Enum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"


@dataclass(frozen=True)
class EligibilityResult:
    status: EligibilityStatus
    message: str

    @property
    def is_eligible(self) -> bool:
        return self.status is EligibilityStatus.ELIGIBLE


@dataclass(frozen=True)
class ActivityState:
    confirmed_at_ms: int
    baseline_last_trade_time_ms: Optional[int] = None


def _format_decimal(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _lesson2(referral: ReferralData) -> EligibilityResult:
    deposit_ok = referral.deposit_amount >= Decimal("100")
    first_trade_ok = referral.first_trade_time is not None
    if deposit_ok and first_trade_ok:
        return EligibilityResult(
            EligibilityStatus.ELIGIBLE,
            f"✅ Условие выполнено: депозит {_format_decimal(referral.deposit_amount)} USDT, "
            "первая сделка подтверждена.",
        )

    missing = []
    if not deposit_ok:
        missing.append(
            f"пополнить счёт минимум до 100 USDT "
            f"(сейчас {_format_decimal(referral.deposit_amount)} USDT)"
        )
    if not first_trade_ok:
        missing.append("совершить первую сделку")
    return EligibilityResult(
        EligibilityStatus.INELIGIBLE,
        "❌ Условие пока не выполнено: " + " и ".join(missing) + ".",
    )


def _volume(referral: ReferralData, threshold: Decimal, lesson_number: int) -> EligibilityResult:
    if referral.trading_amount >= threshold:
        return EligibilityResult(
            EligibilityStatus.ELIGIBLE,
            f"✅ Условие выполнено: торговый объём "
            f"{_format_decimal(referral.trading_amount)} USDT.",
        )
    return EligibilityResult(
        EligibilityStatus.INELIGIBLE,
        f"❌ Торговый объём {_format_decimal(referral.trading_amount)} USDT. "
        f"Для методички №{lesson_number} нужно минимум {_format_decimal(threshold)} USDT.",
    )


def _qualified_invites(count: int, required: int, lesson_number: int) -> EligibilityResult:
    if count >= required:
        return EligibilityResult(
            EligibilityStatus.ELIGIBLE,
            f"✅ Условие выполнено: квалифицированных приглашённых — {count}.",
        )
    return EligibilityResult(
        EligibilityStatus.INELIGIBLE,
        f"❌ Для методички №{lesson_number} нужно квалифицированных приглашённых: "
        f"{required}. Сейчас подтверждено: {count}.",
    )


def _lesson5(
    referral: Optional[ReferralData],
    activity_state: Optional[ActivityState],
    now_ms: int,
) -> EligibilityResult:
    if activity_state is None:
        return EligibilityResult(
            EligibilityStatus.INELIGIBLE,
            "❌ Сначала подтвердите торговую активность через проверку своего MEXC UID. "
            "После первого подтверждения начнётся отсчёт 30 дней.",
        )

    eligible_after = activity_state.confirmed_at_ms + ACTIVITY_PERIOD_MS
    if now_ms < eligible_after:
        remaining_days = max(1, (eligible_after - now_ms + DAY_MS - 1) // DAY_MS)
        return EligibilityResult(
            EligibilityStatus.INELIGIBLE,
            f"⏳ Методичку №5 можно проверить после истечения 30 дней. "
            f"Осталось примерно {remaining_days} дн.",
        )

    if referral is None or referral.last_trade_time is None:
        return EligibilityResult(
            EligibilityStatus.INELIGIBLE,
            "❌ MEXC не подтверждает последнюю торговую активность после контрольной даты.",
        )

    if referral.last_trade_time < eligible_after:
        return EligibilityResult(
            EligibilityStatus.INELIGIBLE,
            "❌ Прошло 30 дней, но MEXC пока не показывает сделку после контрольной даты. "
            "Продолжите торговую активность и повторите проверку.",
        )

    return EligibilityResult(
        EligibilityStatus.ELIGIBLE,
        "✅ MEXC подтвердил торговую активность после истечения 30 дней.",
    )


def evaluate_lesson(
    lesson_number: int,
    referral: Optional[ReferralData] = None,
    *,
    qualified_invites: int = 0,
    activity_state: Optional[ActivityState] = None,
    now_ms: Optional[int] = None,
) -> EligibilityResult:
    if lesson_number == 2:
        if referral is None:
            raise ValueError("Lesson 2 requires MEXC referral data")
        return _lesson2(referral)
    if lesson_number == 3:
        if referral is None:
            raise ValueError("Lesson 3 requires MEXC referral data")
        return _volume(referral, Decimal("300"), 3)
    if lesson_number == 4:
        return _qualified_invites(qualified_invites, 1, 4)
    if lesson_number == 5:
        if now_ms is None:
            raise ValueError("Lesson 5 requires current time")
        return _lesson5(referral, activity_state, now_ms)
    if lesson_number == 6:
        return _qualified_invites(qualified_invites, 2, 6)
    if lesson_number == 7:
        if qualified_invites >= 3:
            return _qualified_invites(qualified_invites, 3, 7)
        if referral is None:
            return EligibilityResult(
                EligibilityStatus.INELIGIBLE,
                f"❌ Для методички №7 нужен торговый объём от 5000 USDT "
                f"или 3 квалифицированных приглашённых. Сейчас приглашённых: {qualified_invites}.",
            )
        volume_result = _volume(referral, Decimal("5000"), 7)
        if volume_result.is_eligible:
            return volume_result
        return EligibilityResult(
            EligibilityStatus.INELIGIBLE,
            volume_result.message
            + f" Альтернатива — 3 квалифицированных приглашённых; сейчас: {qualified_invites}.",
        )
    raise ValueError(f"Unknown lesson number: {lesson_number}")


def lesson_requires_referral_data(lesson_number: int, *, qualified_invites: int = 0) -> bool:
    if lesson_number in (2, 3, 5):
        return True
    if lesson_number == 7:
        return qualified_invites < 3
    return False
