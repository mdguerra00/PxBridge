import os
import asyncio
import logging
from aiohttp import web
from telethon import TelegramClient, events
from telethon.sessions import StringSession

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("telegram-bridge")

API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH")
SESSION_STRING = os.getenv("TELETHON_SESSION_STRING", "").strip()
PERPLEXITY_BOT = os.getenv("PERPLEXITY_BOT_USERNAME", "askplexbot")
BRIDGE_TOKEN = os.getenv("BRIDGE_TOKEN", "")

client = TelegramClient(StringSession(SESSION_STRING or None), API_ID, API_HASH)

pending_lock = asyncio.Lock()
pending_future = None
target_chat_id = None


@client.on(events.NewMessage)
async def on_new_message(event):
    global pending_future, target_chat_id

    if pending_future is None or target_chat_id is None:
        return

    if event.chat_id != target_chat_id:
        return

    sender = await event.get_sender()
    if sender is None or not getattr(sender, "bot", False):
        return

    text = event.raw_text.strip()
    if not text:
        return

    if not pending_future.done():
        pending_future.set_result(text)


async def ensure_authorized():
    await client.connect()

    if not await client.is_user_authorized():
        raise RuntimeError(
            "Telegram session is not authorized. "
            "Generate TELETHON_SESSION_STRING locally and set it in Zeabur."
        )

    me = await client.get_me()
    logger.info("Authorized as %s (%s)", me.first_name, me.id)


async def ask_perplexity_via_telegram(question: str, timeout: int = 90) -> str:
    global pending_future, target_chat_id

    async with pending_lock:
        bot_entity = await client.get_entity(PERPLEXITY_BOT)
        target_chat_id = bot_entity.id
        pending_future = asyncio.get_event_loop().create_future()

        await client.send_message(bot_entity, question)

        try:
            answer = await asyncio.wait_for(pending_future, timeout=timeout)
            return answer
        finally:
            pending_future = None


async def health(request):
    return web.json_response({"ok": True})


async def ask(request):
    if BRIDGE_TOKEN:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {BRIDGE_TOKEN}":
            return web.json_response({"error": "unauthorized"}, status=401)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    question = (data.get("question") or "").strip()
    if not question:
        return web.json_response({"error": "missing_question"}, status=400)

    timeout = int(data.get("timeout", 90))

    try:
        answer = await ask_perplexity_via_telegram(question, timeout=timeout)
        return web.json_response({"ok": True, "answer": answer})
    except asyncio.TimeoutError:
        return web.json_response({"error": "timeout_waiting_perplexity"}, status=504)
    except Exception as e:
        logger.exception("Bridge error")
        return web.json_response({"error": str(e)}, status=500)


async def on_startup(app):
    await ensure_authorized()


async def on_cleanup(app):
    await client.disconnect()


def create_app():
    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_post("/ask", ask)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    if not API_ID or not API_HASH:
        raise RuntimeError("Missing TELEGRAM_API_ID or TELEGRAM_API_HASH")

    if not SESSION_STRING:
        raise RuntimeError("Missing TELETHON_SESSION_STRING")

    app = create_app()
    port = int(os.getenv("PORT", "8080"))
    web.run_app(app, host="0.0.0.0", port=port)
