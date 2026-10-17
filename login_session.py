# login_session.py
# Запускаешь ОДИН РАЗ. После этого появится файл sevenuprelayer.session
# Всё, что нужно — ввести код из Telegram.

import asyncio
from telethon import TelegramClient

# ===== ТВОИ ДАННЫЕ =====
API_ID = 27947766
API_HASH = "c4978ed1ee29460a66b12d8bd7a65dd0"
PHONE = "+15514381585"   # ← номер аккаунта @sevenuprelayer (с кодом страны!)
SESSION_NAME = "sevenuprelayer"  # на выходе будет sevenuprelayer.session


async def main():
    print("📱 Запуск логина в Telegram...")
    print(f"   Сессия будет сохранена как: {SESSION_NAME}.session")

    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    await client.start(phone=PHONE)

    me = await client.get_me()
    print(f"\n✅ Успешно залогинен!")
    print(f"   Имя:      {me.first_name}")
    print(f"   Username: @{me.username}")
    print(f"   ID:       {me.id}")
    print(f"\n📁 Сессия сохранена: {SESSION_NAME}.session")
    print("   Теперь можно запускать gift_listener.py\n")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())