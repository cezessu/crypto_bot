"""Lesson eligibility rules based only on documented public MEXC fields."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Callable, Dict, Optional, Tuple

from mexc_client import ReferralData


class EligibilityStatus(str, Enum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True)
class EligibilityResult:
    status: EligibilityStatus
    message: str

    @property
    def is_eligible(self) -> bool:
        return self.status is EligibilityStatus.ELIGIBLE


@dataclass(frozen=True)
class ConditionDefinition:
    key: str
    lesson_number: int
    evaluator: Optional[Callable[[ReferralData], EligibilityResult]]
    unavailable_message: str = ""


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


UNVERIFIABLE_MESSAGES = {
    "lesson_4_qualified_friend": (
        "ℹ️ Методичка №4 временно недоступна для автоматической проверки: "
        "публичный MEXC API не показывает квалифицированных друзей конкретного UID. "
        "Критерий будет заменён после согласования нового проверяемого условия."
    ),
    "lesson_5_trade_count": (
        "ℹ️ Методичка №5 временно недоступна для автоматической проверки: "
        "публичный MEXC API не возвращает достоверное количество сделок реферала. "
        "Критерий будет заменён после согласования нового проверяемого условия."
    ),
    "lesson_6_qualified_friends": (
        "ℹ️ Методичка №6 временно недоступна для автоматической проверки: "
        "публичный MEXC API не показывает квалифицированных друзей конкретного UID. "
        "Критерий будет заменён после согласования нового проверяемого условия."
    ),
    "lesson_7_qualified_friends": (
        "ℹ️ Альтернативная ветка методички №7 по приглашённым друзьям временно "
        "недоступна для автоматической проверки через публичный MEXC API."
    ),
}


CONDITIONS: Dict[str, ConditionDefinition] = {
    "lesson_2_deposit_and_first_trade": ConditionDefinition(
        "lesson_2_deposit_and_first_trade", 2, _lesson2
    ),
    "lesson_3_trading_volume": ConditionDefinition(
        "lesson_3_trading_volume", 3, lambda referral: _volume(referral, Decimal("300"), 3)
    ),
    "lesson_4_qualified_friend": ConditionDefinition(
        "lesson_4_qualified_friend",
        4,
        None,
        UNVERIFIABLE_MESSAGES["lesson_4_qualified_friend"],
    ),
    "lesson_5_trade_count": ConditionDefinition(
        "lesson_5_trade_count",
        5,
        None,
        UNVERIFIABLE_MESSAGES["lesson_5_trade_count"],
    ),
    "lesson_6_qualified_friends": ConditionDefinition(
        "lesson_6_qualified_friends",
        6,
        None,
        UNVERIFIABLE_MESSAGES["lesson_6_qualified_friends"],
    ),
    "lesson_7_trading_volume": ConditionDefinition(
        "lesson_7_trading_volume", 7, lambda referral: _volume(referral, Decimal("5000"), 7)
    ),
    "lesson_7_qualified_friends": ConditionDefinition(
        "lesson_7_qualified_friends",
        7,
        None,
        UNVERIFIABLE_MESSAGES["lesson_7_qualified_friends"],
    ),
}


LESSON_CONDITIONS: Dict[int, Tuple[str, ...]] = {
    2: ("lesson_2_deposit_and_first_trade",),
    3: ("lesson_3_trading_volume",),
    4: ("lesson_4_qualified_friend",),
    5: ("lesson_5_trade_count",),
    6: ("lesson_6_qualified_friends",),
    7: ("lesson_7_trading_volume", "lesson_7_qualified_friends"),
}


def evaluate_condition(
    condition_key: str,
    referral: Optional[ReferralData],
) -> EligibilityResult:
    definition = CONDITIONS[condition_key]
    if definition.evaluator is None:
        return EligibilityResult(EligibilityStatus.UNVERIFIABLE, definition.unavailable_message)
    if referral is None:
        raise ValueError(f"Condition {condition_key} requires referral data")
    return definition.evaluator(referral)


def evaluate_lesson(
    lesson_number: int,
    referral: Optional[ReferralData] = None,
) -> EligibilityResult:
    condition_keys = LESSON_CONDITIONS[lesson_number]
    results = [evaluate_condition(condition_key, referral) for condition_key in condition_keys]

    eligible_result = next((result for result in results if result.is_eligible), None)
    if eligible_result:
        return eligible_result

    verifiable_results = [
        result for result in results if result.status is not EligibilityStatus.UNVERIFIABLE
    ]
    unavailable_results = [
        result for result in results if result.status is EligibilityStatus.UNVERIFIABLE
    ]
    if verifiable_results:
        message_parts = [result.message for result in verifiable_results]
        message_parts.extend(result.message for result in unavailable_results)
        return EligibilityResult(
            verifiable_results[0].status,
            "\n\n".join(message_parts),
        )

    return unavailable_results[0]


def lesson_requires_referral_data(lesson_number: int) -> bool:
    return CONDITIONS[LESSON_CONDITIONS[lesson_number][0]].evaluator is not None
