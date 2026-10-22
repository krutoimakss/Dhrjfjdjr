"""Запусти ЛОКАЛЬНО один раз, чтобы получить TG_SESSION для Railway."""
from telethon.sessions import StringSession
from telethon.sync import TelegramClient

api_id = int(input("API_ID: ").strip())
api_hash = input("API_HASH: ").strip()

with TelegramClient(StringSession(), api_id, api_hash) as client:
    print("\nTG_SESSION (никому не показывай):\n")
    print(client.session.save())
