import os
import asyncio
import logging
from aiohttp import web
from telethon import TelegramClient, events
from telethon.sessions import StringSession, SQLiteSession

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

# Persistent session file (requires Zeabur volume at /data)
SESSION_FILE = "/data/tg_session"
SESSION_DB = SESSION_FILE + ".session"


def prepare_session():
    """
    Returns session for TelegramClient.
    1. Uses /data/tg_session.session if it exists (auto-updates MTProto keys - never expires).
    2. Otherwise bootstraps from TELETHON_SESSION_STRING and saves to file.
    After first boot the SESSION_STRING env var is no longer needed.
    """
    os.makedirs("/data", exist_ok=True)

    if os.path.exists(SESSION_DB):
        logger.info("Using persistent session file: %s", SESSION_DB)
        return SESSION_FILE

    if not SESSION_STRING:
        raise RuntimeError(
            "No persistent session file and no TELETHON_SESSION_STRING set. "
            "Run generate_session.py to create a session string."
        )

    logger.info("First run: bootstrapping persistent session from SESSION_STRING...")
    try:
        str_sess = StringSession(SESSION_STRING)
        file_sess = SQLiteSession(SESSION_FILE)
        file_sess.set_dc(str_sess.dc_id, str_sess.server_address, str_sess.port)
        file_sess.auth_key = str_sess.auth_key
        file_sess.save()
        logger.info("Persistent session saved to %s", SESSION_DB)
        return SESSION_FILE
    except Exception as e:
        logger.warning("Could not create file session (%s), falling back to StringSession", e)
        return StringSession(SESSION_STRING)


_session = prepare_session()
client = TelegramClient(_session, API_ID, API_HASH)

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
        text = (event.raw_text or "").strip()
        if text.lower().startswith("pensando"):
            return
        pending_future.set_result(text)


async def ensure_authorized():
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError(
            "Telegram session is not authorized. "
            "Delete /data/tg_session.session via Zeabur terminal, "
            "generate a new TELETHON_SESSION_STRING with generate_session.py, "
            "update the variable in Zeabur and redeploy."
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
    app = create_app()
    port = int(os.getenv("PORT", "8080"))
    web.run_app(app, host="0.0.0.0", port=port)
