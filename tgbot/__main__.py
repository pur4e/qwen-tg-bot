"""Точка входа: python -m tgbot. Поднимает поллинг Telegram + health-сервер (для Docker)."""
import asyncio
import logging
import os
import threading

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand

from . import core, db
from .config import BOT_TOKEN
from .groq_api import groq
from .handlers import router

log = logging.getLogger("bot")

COMMANDS = [
    BotCommand(command="start", description="Приветствие"),
]


def _start_health_server() -> None:
    """Крошечный HTTP URL/healthcheck на 7860 — нужен только хостингу (HF Spaces)."""
    import threading
    try:
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Health(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *a):
                pass

        port = int(os.getenv("PORT", "7860"))
        server = HTTPServer(("0.0.0.0", port), Health)
        th = threading.Thread(target=server.serve_forever, daemon=True)
        th.start()
        log.info("Health-сервер слушает порт %s", port)
    except OSError as e:
        log.warning("Health-порт занят/недоступен (%s) — продолжаю без него", e)
    except Exception as e:
        log.warning("Health-сервер не запущен: %s", e)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)

    if not BOT_TOKEN:
        raise SystemExit("❌ Нет токена бота: задайте BOT_TOKEN в .env или секретах хостинга")
    if not groq.api_key:
        raise SystemExit("❌ Нет ключа Groq: задайте GROQ_API_KEY в .env или секретах хостинга")

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    await db.init_db()
    await groq.ensure_default_model()

    me = await bot.get_me()
    core.BOT_USERNAME = me.username
    core.BOT_ID = me.id
    log.info("Бот @%s (id=%s) готов, модель: %s", me.username, me.id, groq.resolved_default)

    _start_health_server()

    try:
        await bot.set_my_commands(COMMANDS)
    except Exception as e:
        log.warning("Не удалось установить меню команд: %s", e)

    try:
        await dp.start_polling(bot, allowed_updates=["message"])
    finally:
        await groq.close()
        await db.close_db()
        log.info("Бот остановлен.")


def run() -> None:
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    run()
