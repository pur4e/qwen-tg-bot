"""Обработчики Telegram: минимум команд, всё остальное — текстом."""
import base64
import logging

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message  # noqa: F401

from . import core, db, ui
from .groq_api import ChatAPIError, groq

log = logging.getLogger("handlers")
router = Router()


def _greet_name(m: Message) -> str:
    u = m.from_user
    for candidate in (u.first_name, u.username and f"@{u.username}"):
        if candidate and candidate.strip():
            return candidate.strip()[:40]
    return "гость"


@router.message(CommandStart())
async def cmd_start(message: Message):
    await db.ensure_user(message.from_user.id, getattr(message.from_user, "username", None))
    await try_or_send(message,
                      f"Что вас интересует, {_greet_name(message)}?",
                      ui.main_kb())


async def try_or_send(message, text, kb=None):
    try:
        return await message.answer(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        return await ui.try_send(message, text)


@router.message(F.voice | F.audio)
async def on_voice(message: Message):
    uid = message.from_user.id
    await db.ensure_user(uid, getattr(message.from_user, "username", None))
    st = await ui.try_send(message, "🎙 Слушаю…")
    try:
        f = message.voice or message.audio
        file = await message.bot.get_file(f.file_id)
        buf = await message.bot.download(file)
        data = buf.getvalue()
        mime = getattr(f, "mime_type", None) or "audio/ogg"
        ext = "mp3" if "mpeg" in mime else ("wav" if "wav" in mime else "ogg")
        text = await groq.transcribe(data, f"sound.{ext}", mime)
    except ChatAPIError as e:
        await ui.try_send(message, f"⚠️ Не удалось расшифровать аудио: {e}")
        return
    except Exception:
        await ui.try_send(message, "⚠️ Не получилось обработать аудио, попробуйте ещё раз.")
        return
    if st:
        try:
            await st.delete()
        except Exception:
            pass
    short = text if len(text) <= 500 else text[:500] + "…"
    await ui.try_send(message, f"🗣 Вы сказали:\n_{short}_")
    await core.generate_reply(message, text=text)


@router.message(F.photo)
async def on_photo(message: Message):
    uid = message.from_user.id
    await db.ensure_user(uid, getattr(message.from_user, "username", None))
    caption = (message.caption or "").strip()
    if not caption:
        caption = "Опиши, что на изображении"
    photo = message.photo[-1]
    file = await message.bot.get_file(photo.file_id)
    buf = await message.bot.download(file)
    b64 = base64.b64encode(buf.getvalue()).decode()
    await core.generate_reply(message, text=caption, photo_b64=b64)


@router.message(F.text)
async def on_text(message: Message):
    text = (message.text or "").strip()
    uid = message.from_user.id
    await db.ensure_user(uid, getattr(message.from_user, "username", None))

    if text == ui.NEW_CHAT_BTN:
        await db.clear_messages(uid)
        await ui.try_send(message, "🔄 Начал новый диалог. Что вас интересует?")
        return

    is_private = message.chat.type == ChatType.PRIVATE
    if not is_private:
        mentioned = core.BOT_USERNAME and f"@{core.BOT_USERNAME.lower()}" in text.lower()
        replying_bot = (message.reply_to_message and message.reply_to_message.from_user
                        and message.reply_to_message.from_user.id == core.BOT_ID)
        if not (mentioned or replying_bot):
            return

    if text.startswith("/"):
        await ui.try_send(message, "Здесь нет команд и меню — просто напишите сообщение текстом 🙂")
        return

    await core.generate_reply(message)
