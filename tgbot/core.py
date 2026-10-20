"""Ядро: диалог с инструментами (generate_image, web_search) и стриминг ответа."""
import asyncio
import json
import logging
import time
from typing import Optional

import httpx
from aiogram.types import BufferedInputFile, Message

from . import db, prompts
from .config import (DEFAULT_TEMPERATURE, EDIT_INTERVAL, HISTORY_MAX_CHARS,
                     MAX_OUTPUT_TOKENS, SEARCH_RESULTS)
from .groq_api import ChatAPIError, groq
from .ui import split_text, try_edit, try_send
from .websearch import format_results, search_web

log = logging.getLogger("core")

_locks: dict = {}
BOT_USERNAME: Optional[str] = None
BOT_ID: Optional[int] = None

IMG_HTTP = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=170.0))


def lock_for(uid: int) -> asyncio.Lock:
    return _locks.setdefault(uid, asyncio.Lock())


async def _typing_loop(bot, chat_id: int, stop_flag: asyncio.Event):
    try:
        while not stop_flag.is_set():
            try:
                await bot.send_chat_action(chat_id, "typing")
            except Exception:
                return
            try:
                await asyncio.wait_for(stop_flag.wait(), timeout=4.5)
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        pass


def _visible_preview(buf: str) -> str:
    if len(buf) <= 3800:
        return buf
    cut = buf.rfind("\n", 0, 3800)
    if cut < 2000:
        cut = 3800
    return buf[:cut] + "\n\n…"


def _build_messages(history: list, user_text: str, photo_b64: str = "") -> list:
    msgs = [{"role": "system", "content": prompts.system_prompt()}]
    msgs += [{"role": m["role"], "content": m["content"]} for m in history]
    content = user_text
    if photo_b64:
        content = [
            {"type": "text", "text": user_text},
            {"type": "image_url",
             "image_url": {"url": f"data:image/jpeg;base64,{photo_b64}"}},
        ]
    msgs.append({"role": "user", "content": content})
    return msgs


def _history_chars(history: list) -> int:
    return sum(len(m["content"]) for m in history)


async def _trim_history(uid: int) -> list:
    """История хранится в БД; если разрослась — тихо режем самую старую часть."""
    history = await db.get_messages(uid)
    if _history_chars(history) <= HISTORY_MAX_CHARS:
        return history
    while len(history) > 4 and _history_chars(history) > HISTORY_MAX_CHARS // 2:
        drop = history[:max(1, len(history) // 2)]
        await db.delete_messages(uid, [m["id"] for m in drop])
        history = await db.get_messages(uid)
    return history


async def generate_image_bytes(prompt: str) -> bytes:
    from urllib.parse import quote
    url = ("https://image.pollinations.ai/prompt/" + quote(prompt.strip()[:600])
           + "?width=1024&height=1024&nologo=true&model=flux&seed="
           + str(int(time.time() * 1000) % 999999))
    r = await IMG_HTTP.get(url)
    r.raise_for_status()
    return r.content


async def generate_reply(message: Message, *, text: Optional[str] = None,
                         photo_b64: str = "") -> None:
    uid = message.from_user.id
    lock = lock_for(uid)
    if lock.locked():
        await message.reply("⏳ Минутку, я ещё не закончил с предыдущим сообщением.")
        return
    async with lock:
        await _generate(message, text=text, photo_b64=photo_b64)


async def _generate(message: Message, *, text, photo_b64):
    uid = message.from_user.id
    await db.ensure_user(uid, getattr(message.from_user, "username", None))
    user_text = (text if text else (message.text or "")).strip()
    if photo_b64 and not user_text:
        user_text = "Опиши, что на изображении; если есть вопрос — отвечай по делу."
    if not user_text:
        return

    history = await _trim_history(uid)
    model = groq.resolved_default or await groq.ensure_default_model()
    temperature = DEFAULT_TEMPERATURE

    messages = _build_messages(history, user_text, photo_b64)

    placeholder = await message.reply("💭 Думаю…")
    typing_stop = asyncio.Event()
    typing_task = asyncio.create_task(_typing_loop(message.bot, message.chat.id, typing_stop))

    try:
        full_text = ""
        for round_no in range(3):
            info: dict = {"uid": uid}
            buf, tool_calls = await _stream_round(placeholder, messages, model,
                                                  temperature, info)
            if not tool_calls or round_no == 2:
                full_text = buf
                break
            # --- выполняем инструменты
            messages.append({"role": "assistant",
                             "content": (buf or None),
                             "tool_calls": tool_calls})
            for tc in tool_calls:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                if name == "generate_image":
                    prompt = (args.get("prompt") or "").strip()
                    await try_edit(placeholder, "🎨 Генерирую картинку…")
                    try:
                        data = await generate_image_bytes(prompt)
                        await message.answer_photo(
                            BufferedInputFile(data, filename="img.jpg"))
                        result = '{"status":"ok","note":"изображение отправлено пользователю"}'
                    except Exception as e:
                        log.warning("img error: %s", e)
                        result = '{"status":"error","note":"сервис изображений недоступен"}'
                elif name == "web_search":
                    q = (args.get("query") or "").strip()
                    await try_edit(placeholder, "🌐 Ищу в интернете…")
                    try:
                        res = await search_web(q, SEARCH_RESULTS)
                        result = format_results(res, q) if res else "по запросу ничего не нашлось"
                    except Exception as e:
                        log.warning("search error: %s", e)
                        result = "поиск временно недоступен"
                else:
                    result = '{"status":"error","note":"неизвестный инструмент"}'
                messages.append({"role": "tool", "tool_call_id": tc.get("id") or "",
                                 "content": result[:6000]})

        final_text = (full_text or "").strip()
        if not final_text:
            final_text = "🤔 Пусто вышло. Попробуйте сформулировать ещё раз."

        parts = split_text(final_text) or [""]
        try:
            await placeholder.edit_text(parts[0], parse_mode=None,
                                        disable_web_page_preview=True)
        except Exception:
            await try_send(message, parts[0])
        for extra in parts[1:]:
            await try_send(message, extra)

        await db.add_message(uid, "user", user_text)
        await db.add_message(uid, "assistant", final_text)

    except ChatAPIError as e:
        log.error("Ошибка Groq: %s (kind=%s)", e, e.kind)
        txt = _api_error_text(e)
        try:
            await placeholder.edit_text(txt, parse_mode=None)
        except Exception:
            await try_send(message, txt)
    except Exception:
        log.exception("Непредвиденная ошибка в generate")
        try:
            await try_send(message, "😅 Что-то пошло не так. Попробуйте ещё раз.")
        except Exception:
            pass
    finally:
        typing_stop.set()
        try:
            typing_task.cancel()
        except Exception:
            pass


def _api_error_text(e: ChatAPIError) -> str:
    if e.kind == "context":
        return "📖 Диалог стал слишком длинным. Нажмите «🆕 Начать новый чат», чтобы начать заново."
    if e.kind == "rate":
        return "🚦 Слишком много запросов. Подождите немного и напишите снова."
    if e.kind == "network":
        return "📡 Не удалось связаться с Groq. Проверьте интернет и попробуйте ещё раз."
    return "⚠️ Ошибка со стороны ИИ. Попробуйте написать ещё раз."


async def _stream_round(placeholder: Message, messages: list, model: str,
                        temperature: float, info: dict):
    """Один раунд стриминга. Возвращает (text, tool_calls)."""
    buf = ""
    last_edit_t = 0.0
    last_edit_len = -1
    async for delta in groq.chat_stream(messages, model, temperature,
                                        MAX_OUTPUT_TOKENS, info=info,
                                        tools=prompts.TOOLS):
        buf += delta
        now = time.monotonic()
        if now - last_edit_t >= EDIT_INTERVAL and len(buf) != last_edit_len:
            preview = _visible_preview(buf)
            if preview:
                await try_edit(placeholder, preview)
                last_edit_t = now
                last_edit_len = len(buf)
    return buf, info.get("tool_calls") or []
