"""Минимальный UI: одна кнопка + безопасная отправка/правка сообщений."""
import asyncio
import logging

from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from .config import TG_CHUNK, EDIT_INTERVAL

log = logging.getLogger("ui")

NEW_CHAT_BTN = "🆕 Начать новый чат"


def main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        keyboard=[[KeyboardButton(text=NEW_CHAT_BTN)]],
    )


def split_text(text: str, limit: int = TG_CHUNK) -> list:
    text = (text or "").strip()
    if len(text) <= limit:
        return [text] if text else [""]
    parts, buf = [], ""
    for para in text.split("\n\n"):
        while len(para) > limit:
            cut = para.rfind("\n", 0, limit)
            if cut < limit // 2:
                cut = limit
            piece, para = para[:cut], para[cut:].lstrip()
            if buf:
                parts.append(buf)
                buf = ""
            parts.append(piece)
        if not para:
            continue
        candidate = (buf + "\n\n" + para) if buf else para
        if len(candidate) <= limit:
            buf = candidate
        else:
            if buf:
                parts.append(buf)
            buf = para
    if buf:
        parts.append(buf)
    return parts or [""]


def _not_modified(e: Exception) -> bool:
    return "message is not modified" in str(e).lower()


async def try_edit(msg, text: str) -> None:
    """Правит сообщение: сначала Markdown, при ошибке парсинга — чистый текст."""
    for _ in range(3):
        try:
            await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN,
                                disable_web_page_preview=True)
            return
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 0.2)
        except TelegramBadRequest as e:
            if _not_modified(e):
                return
            s = str(e).lower()
            if "parse" in s or "entity" in s:
                try:
                    await msg.edit_text(text, disable_web_page_preview=True)
                    return
                except TelegramBadRequest:
                    return
            if "message is not modified" in s:
                return
            return
    return


async def try_send(message, text: str):
    for _ in range(3):
        try:
            return await message.answer(text, parse_mode=ParseMode.MARKDOWN,
                                        disable_web_page_preview=True)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 0.2)
        except TelegramBadRequest as e:
            if _not_modified(e):
                return None
            s = str(e).lower()
            if "parse" in s or "entity" in s:
                try:
                    return await message.answer(text, disable_web_page_preview=True)
                except TelegramBadRequest:
                    return None
            return await message.answer(text, disable_web_page_preview=True)
    return await message.answer(text, disable_web_page_preview=True)
