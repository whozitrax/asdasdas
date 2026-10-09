from __future__ import annotations
import aiohttp, asyncio, gzip, json, logging, re
from html import unescape
from telegram import Bot, OwnedGiftUnique
logger=logging.getLogger(__name__)
_OG_IMAGE_RE = re.compile(r"<meta[^>]+(?:property|name)=[\"\'](?:og:image|twitter:image)[\"\'][^>]+content=[\"\']([^\"\']+)[\"\']", re.IGNORECASE)
_OG_IMAGE_RE_REVERSED = re.compile(r"<meta[^>]+content=[\"\']([^\"\']+)[\"\'][^>]+(?:property|name)=[\"\'](?:og:image|twitter:image)[\"\']", re.IGNORECASE)
_FRAGMENT_URL_RE = re.compile(r"https://nft\.fragment\.com/gift/[A-Za-z0-9_.\-]+", re.IGNORECASE)
async def _public_gift_preview_image(gift_url: str) -> tuple[bytes, str] | None:
    if not gift_url.startswith("https://t.me/nft/"):
        return None

    timeout = aiohttp.ClientTimeout(total=10, connect=5)
    headers = {
        "User-Agent": "Mozilla/5.0 GiftRelayer/1.0",
        "Accept": "text/html,application/xhtml+xml",
    }
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(gift_url, allow_redirects=True) as page_resp:
                if page_resp.status != 200:
                    return None
                page_text = await page_resp.text(errors="ignore")

            match = _OG_IMAGE_RE.search(page_text) or _OG_IMAGE_RE_REVERSED.search(page_text)
            if not match:
                return None

            image_url = unescape(match.group(1)).strip()
            if image_url.startswith("//"):
                image_url = "https:" + image_url
            if not image_url.startswith(("https://", "http://")):
                return None

            async with session.get(image_url, allow_redirects=True) as image_resp:
                if image_resp.status != 200:
                    return None
                data = await image_resp.read()
                if not data:
                    return None
                content_type = (image_resp.headers.get("Content-Type") or "image/webp").split(";", 1)[0].strip()
                if not content_type.startswith("image/"):
                    content_type = "image/webp"
                return data, content_type
    except Exception as exc:
        logger.debug("Public gift preview unavailable url=%s: %s", gift_url, exc)
        return None

async def _fragment_gift_lottie(gift_url: str, gift_slug: str) -> bytes | None:
    """Best effort: transparent animated model (lottie JSON) from the public CDN."""
    candidates = [f"https://nft.fragment.com/gift/{gift_slug.lower()}.lottie.json"]
    headers = {"User-Agent": "Mozilla/5.0 GiftRelayer/1.0"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8, connect=4), headers=headers) as session:
            try:
                async with session.get(gift_url, allow_redirects=True) as resp:
                    if resp.status == 200:
                        page = await resp.text(errors="ignore")
                        for m in _FRAGMENT_URL_RE.finditer(page):
                            u = m.group(0)
                            if u.lower().endswith((".json", ".tgs")) and u not in candidates:
                                candidates.insert(0, u)
            except Exception as exc:
                logger.debug("Gift page fetch failed: %s", exc)
            for url in candidates:
                try:
                    async with session.get(url) as r:
                        if r.status != 200:
                            continue
                        raw = await r.read()
                    if url.lower().endswith(".tgs") or raw[:2] == b"\x1f\x8b":
                        raw = gzip.decompress(raw)
                    json.loads(raw.decode("utf-8"))
                    logger.info("GIFT MEDIA FOUND | source=fragment | url=%s", url)
                    return raw
                except Exception as exc:
                    logger.debug("Fragment candidate failed %s: %s", url, exc)
    except Exception as exc:
        logger.debug("Fragment lottie lookup failed: %s", exc)
    return None

async def _find_gift_model_sticker(bot: Bot, offer, auth_user_id: int):
    """Find the original Telegram model sticker for this exact collectible.

    Best source for a Business offer is getBusinessAccountGifts(): it returns
    the managed account's unique gifts together with the model sticker. That
    sticker is the clean Telegram asset (TGS/WEBM/WEBP), usually without the
    collectible-card background baked into the public preview.
    """
    wanted = (offer.gift_slug or "").casefold()

    def extract_model_sticker(owned, source: str):
        if not isinstance(owned, OwnedGiftUnique):
            return None

        gift = getattr(owned, "gift", None)
        gift_name = (getattr(gift, "name", "") or "").casefold()
        if f"{gift_name}-{getattr(gift, 'number', '')}".casefold() != wanted:
            return None

        model = getattr(gift, "model", None)
        sticker = getattr(model, "sticker", None)
        if sticker:
            logger.info(
                "GIFT MODEL FOUND | offer_id=%s | source=%s | gift=%s | model=%s | animated=%s | video=%s | %sx%s",
                offer.id,
                source,
                getattr(gift, "name", "-"),
                getattr(model, "name", "-"),
                getattr(sticker, "is_animated", False),
                getattr(sticker, "is_video", False),
                getattr(sticker, "width", "-"),
                getattr(sticker, "height", "-"),
            )
        return sticker

    # 1) Managed Business account: preferred and does not depend on profile visibility.
    connection_id = getattr(offer, "business_connection_id", None)
    if connection_id:
        try:
            offset: str | None = None
            while True:
                page = await bot.get_business_account_gifts(
                    business_connection_id=connection_id,
                    exclude_unlimited=True,
                    exclude_limited_upgradable=True,
                    exclude_limited_non_upgradable=True,
                    exclude_from_blockchain=True,
                    exclude_unique=False,
                    offset=offset,
                    limit=100,
                )
                for owned in page.gifts:
                    sticker = extract_model_sticker(owned, "business_account")
                    if sticker:
                        return sticker

                if not page.next_offset:
                    break
                offset = page.next_offset
        except Exception as exc:
            logger.warning(
                "GIFT MODEL BUSINESS LOOKUP FAILED | offer_id=%s | gift=%s | error=%s",
                offer.id,
                offer.gift_slug,
                exc,
            )

    # 2) User gift lists: fallback when Business gift access isn't available.
    candidate_ids: list[int] = []
    if offer.seller_id:
        candidate_ids.append(offer.seller_id)
    if offer.source_type == "BUSINESS" and offer.chat_id and offer.chat_id not in candidate_ids:
        candidate_ids.append(offer.chat_id)
    if auth_user_id not in candidate_ids:
        candidate_ids.append(auth_user_id)
    if offer.creator_id not in candidate_ids:
        candidate_ids.append(offer.creator_id)

    for uid in candidate_ids:
        try:
            offset: str | None = None
            while True:
                page = await bot.get_user_gifts(
                    user_id=uid,
                    exclude_unlimited=True,
                    exclude_limited_upgradable=True,
                    exclude_limited_non_upgradable=True,
                    exclude_from_blockchain=True,
                    exclude_unique=False,
                    offset=offset,
                    limit=100,
                )
                for owned in page.gifts:
                    sticker = extract_model_sticker(owned, f"user:{uid}")
                    if sticker:
                        return sticker

                if not page.next_offset:
                    break
                offset = page.next_offset
        except Exception as exc:
            logger.debug(
                "Gift model unavailable user=%s gift=%s: %s",
                uid,
                offer.gift_slug,
                exc,
            )

    logger.warning(
        "GIFT MODEL NOT FOUND | offer_id=%s | gift=%s",
        offer.id,
        offer.gift_slug,
    )
    return None
