from telethon.sync import TelegramClient
from telethon.sessions import StringSession

API_ID = 27947766
API_HASH = "c4978ed1ee29460a66b12d8bd7a65dd0"

with TelegramClient(StringSession(), API_ID, API_HASH) as client:
    print(client.session.save())