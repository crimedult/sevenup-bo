# gift_listener.py
# Слушает входящие подарки на @sevenuprelayer.
# Определяет цену через TelegramGifts (floor price маркетплейсов).
# Переводит в звёзды и начисляет баланс.

import asyncio
import logging
from telethon import TelegramClient, events, functions

from database import add_balance, upsert_user

# ===== НАСТРОЙКИ =====
API_ID = 27947766
API_HASH = "c4978ed1ee29460a66b12d8bd7a65dd0"
SESSION_NAME = "sevenuprelayer"

# Курс: сколько звёзд даём за $1 стоимости подарка
STARS_PER_USD = 100

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("gift_listener")

client = TelegramClient(SESSION_NAME, API_ID, API_HASH)


# ===== ЦЕНА ПОДАРКА =====
try:
    from TelegramGifts import TelegramGifts
    gifts_lib = TelegramGifts()
    log.info("✅ TelegramGifts загружен")
except Exception as e:
    gifts_lib = None
    log.warning(f"⚠️ TelegramGifts не загружен: {e}")


async def get_gift_price_usd(gift) -> float:
    """Определяет цену подарка в USD."""
    try:
        # 1) Если Telegram отдал value_usd_amount напрямую
        if hasattr(gift, "value_usd_amount") and gift.value_usd_amount:
            return gift.value_usd_amount / 100

        # 2) Пробуем через uniqueStarGiftValueInfo
        if hasattr(gift, "slug") and gift.slug:
            try:
                info = await client(functions.payments.GetUniqueStarGiftValueInfoRequest(
                    slug=gift.slug
                ))
                # value в минимальных единицах валюты (центы)
                if info.value:
                    return info.value / 100
                # floor_price в звёздах → USD по курсу ~0.015
                if info.floor_price:
                    return info.floor_price * 0.015
            except Exception as e:
                log.warning(f"uniqueStarGiftValueInfo failed: {e}")

        # 3) Fallback: TelegramGifts (floor price маркетплейсов)
        if gifts_lib and hasattr(gift, "title") and gift.title:
            try:
                info = gifts_lib.get_gift(gift.title)
                # tgmrkt_price_ton — цена в TON, переводим в USD (~5 USD за 1 TON)
                ton_price = info.get("prices", {}).get("tgmrkt_price_ton", 0)
                if ton_price:
                    return ton_price * 5.0
            except Exception as e:
                log.warning(f"TelegramGifts failed: {e}")

        return 0.0
    except Exception as e:
        log.error(f"Price extraction error: {e}")
        return 0.0


# ===== СЛУШАТЕЛЬ =====
@client.on(events.NewMessage)
async def on_new_message(event):
    msg = event.message

    # Подарки приходят как service message с action.gift
    if not (msg.action and hasattr(msg.action, "gift")):
        return

    gift = msg.action.gift
    sender = await event.get_sender()

    if not sender:
        return

    log.info(f"🎁 Подарок от {sender.id} (@{getattr(sender, 'username', '?')})")
    log.info(f"   Gift: {getattr(gift, 'title', '?')} / slug={getattr(gift, 'slug', '?')}")

    price_usd = await get_gift_price_usd(gift)
    log.info(f"   Price: ${price_usd:.2f}")

    if price_usd <= 0:
        log.warning("   ⚠️ Цена не определена — подарок не зачислен")
        return

    stars = int(price_usd * STARS_PER_USD)
    log.info(f"   Начисляем: {stars} ⭐")

    await upsert_user({
        "user_id": sender.id,
        "username": getattr(sender, "username", None),
        "first_name": getattr(sender, "first_name", ""),
        "last_name": getattr(sender, "last_name", ""),
    })
    new_balance = await add_balance(sender.id, stars)

    log.info(f"   ✅ Баланс {sender.id}: {new_balance} ⭐")


async def main():
    await client.start()
    me = await client.get_me()
    log.info(f"🎧 Слушаю подарки на @{me.username} (id={me.id})")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())