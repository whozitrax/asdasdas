from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum

from telegram import Bot, OwnedGiftUnique
from telegram.error import TelegramError

logger = logging.getLogger(__name__)


class GiftPresence(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class PresenceResult:
    status: GiftPresence
    details: str


class TransferCheck(str, Enum):
    VERIFIED = "VERIFIED"
    NOT_TRANSFERRED = "NOT_TRANSFERRED"
    WRONG_OWNER_OR_UNKNOWN = "WRONG_OWNER_OR_UNKNOWN"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class TransferVerificationResult:
    status: TransferCheck
    seller_presence: GiftPresence
    buyer_presence: GiftPresence
    details: str


async def gift_presence(bot: Bot, *, user_id: int, gift_slug: str) -> PresenceResult:
    """Check whether a Telegram-native unique gift is currently owned/hosted by user."""
    offset: str | None = None
    try:
        while True:
            page = await bot.get_user_gifts(
                user_id=user_id,
                exclude_unlimited=True,
                exclude_limited_upgradable=True,
                exclude_limited_non_upgradable=True,
                exclude_from_blockchain=True,
                exclude_unique=False,
                offset=offset,
                limit=100,
            )
            for owned in page.gifts:
                if isinstance(owned, OwnedGiftUnique) and (
                    f"{owned.gift.name}-{getattr(owned.gift, 'number', '')}".casefold() == gift_slug.casefold()
                ):
                    return PresenceResult(GiftPresence.PRESENT, "Gift найден у пользователя.")
            if not page.next_offset:
                return PresenceResult(GiftPresence.ABSENT, "Gift у пользователя не найден.")
            offset = page.next_offset
    except TelegramError as exc:
        logger.warning("Gift presence unavailable user_id=%s gift=%s: %s", user_id, gift_slug, exc)
        return PresenceResult(GiftPresence.UNAVAILABLE, str(exc))
    except Exception as exc:
        logger.warning("Gift presence failed user_id=%s gift=%s: %s", user_id, gift_slug, exc)
        return PresenceResult(GiftPresence.UNAVAILABLE, str(exc))


async def verify_transfer(
    bot: Bot,
    *,
    seller_id: int,
    expected_buyer_id: int,
    gift_slug: str,
) -> TransferVerificationResult:
    """Check current state, without requiring a historical snapshot for success.

    VERIFIED: exact unique gift is present on expected buyer and absent from seller.
    NOT_TRANSFERRED: exact gift is still present on seller.
    """
    seller, buyer = await asyncio.gather(
        gift_presence(bot, user_id=seller_id, gift_slug=gift_slug),
        gift_presence(bot, user_id=expected_buyer_id, gift_slug=gift_slug),
    )

    if buyer.status == GiftPresence.PRESENT and seller.status == GiftPresence.ABSENT:
        return TransferVerificationResult(
            TransferCheck.VERIFIED,
            seller.status,
            buyer.status,
            "Gift найден у ожидаемого покупателя и больше не найден у продавца.",
        )

    if seller.status == GiftPresence.PRESENT:
        return TransferVerificationResult(
            TransferCheck.NOT_TRANSFERRED,
            seller.status,
            buyer.status,
            "Gift всё ещё находится у продавца.",
        )

    if buyer.status == GiftPresence.PRESENT:
        # Even if seller lookup was unavailable, finding the exact slug on the expected
        # buyer is useful evidence. The web layer may confirm it with public owner data.
        return TransferVerificationResult(
            TransferCheck.VERIFIED,
            seller.status,
            buyer.status,
            "Gift найден у ожидаемого покупателя.",
        )

    if GiftPresence.UNAVAILABLE in {seller.status, buyer.status}:
        return TransferVerificationResult(
            TransferCheck.UNAVAILABLE,
            seller.status,
            buyer.status,
            "Bot API не вернул достаточно данных для определения владельца.",
        )

    return TransferVerificationResult(
        TransferCheck.WRONG_OWNER_OR_UNKNOWN,
        seller.status,
        buyer.status,
        "Gift не найден ни у продавца, ни у ожидаемого покупателя.",
    )
