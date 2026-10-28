"""Find Telegram chat IDs for a bot token set in TELEGRAM_BOT_TOKEN.

Run after sending /start to your bot in Telegram. The script never prints the
bot token and does not deliberately acknowledge/consume updates.
"""
import os
import sys
import requests


def main() -> int:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("ERROR: Set TELEGRAM_BOT_TOKEN in this PowerShell window first.")
        return 2

    url = f"https://api.telegram.org/bot{token}/getUpdates"
    try:
        response = requests.get(url, params={"limit": 100, "timeout": 0}, timeout=15)
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"ERROR: Could not query Telegram ({type(exc).__name__}).")
        return 3

    if not response.ok or not isinstance(data, dict) or not data.get("ok"):
        description = data.get("description", "Telegram API returned an error") if isinstance(data, dict) else "Invalid Telegram response"
        print(f"ERROR: {description}")
        print("Check the token and whether a webhook is configured for this bot. Do not paste the token into chat.")
        return 4

    found = {}
    for update in data.get("result", []):
        message = update.get("message") or update.get("edited_message")
        if not isinstance(message, dict):
            continue
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            continue
        chat_type = chat.get("type", "unknown")
        from_user = message.get("from") or {}
        text = str(message.get("text", ""))
        if chat_type == "private" and text.strip().startswith("/start"):
            found[str(chat_id)] = {
                "chat_id": str(chat_id),
                "type": chat_type,
                "username": from_user.get("username") or "(no username)",
                "first_name": from_user.get("first_name") or "(unknown)",
            }

    if not found:
        print("No private /start message found yet.")
        print("Open your bot in Telegram, press Start or send /start, then rerun this script.")
        print("If you already did that, this bot may use a webhook or have too many pending updates.")
        return 1

    print("Private chats that sent /start to your bot:")
    for item in found.values():
        print(f"  chat_id: {item['chat_id']} | name: {item['first_name']} | username: {item['username']}")
    print("\nUse the chat_id for YOUR chat as TELEGRAM_TEST_CHAT_ID in the same PowerShell session.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
