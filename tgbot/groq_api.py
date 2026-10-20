"""Клиент Groq API (OpenAI-совместимый): стриминг чата с tool calls, модели, аудио."""
import asyncio
import json
import logging
from typing import AsyncIterator, Optional

import httpx

from .config import GROQ_API_KEY, DEFAULT_MODEL, TRANSCRIBE_MODEL

log = logging.getLogger("groq")

REASONING_HINTS = ("qwen", "gpt-oss", "deepseek")
NON_CHAT_HINTS = ("whisper", "tts", "orpheus", "guard", "safeguard", "playai")


class ChatAPIError(Exception):
    """kind: generic | model | context | param | network | rate"""

    def __init__(self, message: str, kind: str = "generic", status: Optional[int] = None):
        super().__init__(message)
        self.kind = kind
        self.status = status


def _err_kind(text: str) -> str:
    t = text.lower()
    if "reasoning_format" in t or "unsupported parameter" in t or "not a valid parameter" in t:
        return "param"
    if ("model" in t and ("decommission" in t or "not found" in t or "does not exist" in t
                          or "invalid model" in t)) or "model_not_found" in t:
        return "model"
    if "context" in t and ("length" in t or "token" in t or "reduce" in t or "window" in t):
        return "context"
    if "rate limit" in t or "too many requests" in t:
        return "rate"
    return "generic"


class GroqClient:
    BASE = "https://api.groq.com/openai/v1"
    TIMEOUT = httpx.Timeout(20.0, read=300.0, write=60.0)
    MAX_ATTEMPTS = 4

    def __init__(self, api_key: str = "", default_model: str = ""):
        self.api_key = api_key or GROQ_API_KEY
        self.default_model = default_model or DEFAULT_MODEL
        self.resolved_default: Optional[str] = None
        self.last_model_used: Optional[str] = None
        self._client: Optional[httpx.AsyncClient] = None
        self._models: list = []
        self._models_ts: float = 0.0

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.BASE,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.TIMEOUT,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------ модели

    async def list_models(self, force: bool = False) -> list:
        import time
        if not force and self._models and time.time() - self._models_ts < 3600:
            return self._models
        r = await self.client.get("/models")
        if r.status_code != 200:
            raise ChatAPIError(f"Не удалось получить список моделей (HTTP {r.status_code})")
        data = r.json().get("data", [])
        self._models = [
            m for m in data
            if not any(h in m.get("id", "").lower() for h in NON_CHAT_HINTS)
        ]
        self._models_ts = time.time()
        return self._models

    async def ensure_default_model(self) -> str:
        if self.resolved_default:
            return self.resolved_default
        model = self.default_model
        try:
            ids = [m["id"] for m in await self.list_models(force=True) if m.get("active", True)]
        except Exception:
            ids = []
        if ids and model not in ids:
            qwen = [i for i in ids if "qwen" in i.lower()]
            pick = qwen[0] if qwen else (ids[0] if ids else model)
            log.warning("Модель %s недоступна, использую %s вместо неё", self.default_model, pick)
            model = pick
        self.resolved_default = model
        log.info("Модель по умолчанию: %s", model)
        return model

    # ------------------------------------------------------------------ чат

    @staticmethod
    def _payload(messages, model, temperature, max_tokens, stream: bool,
                 reasoning: bool, tools: Optional[list] = None) -> dict:
        p = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": stream,
        }
        if max_tokens:
            p["max_tokens"] = max_tokens
        if reasoning:
            p["reasoning_format"] = "hidden"
        if tools:
            p["tools"] = tools
            p["tool_choice"] = "auto"
        return p

    async def chat_stream(
        self,
        messages: list,
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        stop_event: Optional[asyncio.Event] = None,
        info: Optional[dict] = None,
        tools: Optional[list] = None,
    ) -> AsyncIterator[str]:
        """Генератор дельт текста. info: requested/model_used/finish_reason/tool_calls."""
        if info is None:
            info = {}
        info["requested"] = model
        model = model or self.resolved_default or self.default_model
        use_reasoning = any(h in model.lower() for h in REASONING_HINTS)
        payload = self._payload(messages, model, temperature, max_tokens, True,
                                use_reasoning, tools)

        attempt = 0
        backoff = 1.5
        while attempt < self.MAX_ATTEMPTS:
            if stop_event is not None and stop_event.is_set():
                return
            attempt += 1
            try:
                async with self.client.stream("POST", "/chat/completions", json=payload) as resp:
                    if resp.status_code == 200:
                        info["model_used"] = payload["model"]
                        self.last_model_used = payload["model"]
                        tool_slots: dict = {}
                        done = False
                        async for line in resp.aiter_lines():
                            if stop_event is not None and stop_event.is_set():
                                info["aborted"] = True
                                done = True
                                break
                            line = line.strip()
                            if not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if data == "[DONE]":
                                done = True
                                break
                            try:
                                obj = json.loads(data)
                            except (ValueError, json.JSONDecodeError):
                                continue
                            if "error" in obj:
                                raise ChatAPIError(
                                    obj["error"].get("message", "stream error"),
                                    kind=_err_kind(str(obj["error"])),
                                )
                            try:
                                choice = obj["choices"][0]
                            except (KeyError, IndexError, TypeError):
                                continue
                            if choice.get("finish_reason"):
                                info["finish_reason"] = choice["finish_reason"]
                            delta = choice.get("delta", {})
                            for tc in delta.get("tool_calls") or []:
                                idx = tc.get("index", 0)
                                slot = tool_slots.setdefault(
                                    idx, {"id": None, "type": "function",
                                          "function": {"name": "", "arguments": ""}})
                                if tc.get("id"):
                                    slot["id"] = tc["id"]
                                fn = tc.get("function") or {}
                                if fn.get("name"):
                                    slot["function"]["name"] += fn["name"]
                                if fn.get("arguments"):
                                    slot["function"]["arguments"] += fn["arguments"]
                            piece = delta.get("content")
                            if piece:
                                yield piece
                        if tool_slots:
                            info["tool_calls"] = [tool_slots[i] for i in sorted(tool_slots)]
                        _ = done
                        return
                    body = (await resp.aread()).decode("utf-8", errors="ignore")
                    kind = _err_kind(body)
                    if resp.status_code == 429:
                        retry_after = resp.headers.get("retry-after")
                        delay = (float(retry_after)
                                 if retry_after and retry_after.replace(".", "").isdigit()
                                 else backoff)
                        log.warning("429 rate limit, жду %.1fс (попытка %s)", delay, attempt)
                        if stop_event is not None and stop_event.is_set():
                            return
                        await asyncio.sleep(delay)
                        backoff *= 1.7
                        continue
                    if resp.status_code >= 500:
                        if attempt >= self.MAX_ATTEMPTS:
                            raise ChatAPIError("Groq недоступен (попробуйте позже)",
                                               kind="network", status=resp.status_code)
                        await asyncio.sleep(backoff)
                        backoff *= 1.7
                        continue
                    err = ChatAPIError(body, kind=kind, status=resp.status_code)
                    if kind == "param" and "reasoning_format" in payload:
                        payload.pop("reasoning_format", None)
                        continue
                    if kind == "model":
                        payload["model"] = self.resolved_default or self.default_model
                        use_reasoning = any(h in payload["model"].lower() for h in REASONING_HINTS)
                        payload.pop("reasoning_format", None)
                        info["swapped"] = True
                        continue
                    raise err
            except httpx.HTTPError as e:
                if attempt >= self.MAX_ATTEMPTS:
                    raise ChatAPIError(f"Проблема с сетью: {e}", kind="network") from e
                if stop_event is not None and stop_event.is_set():
                    return
                await asyncio.sleep(backoff)
                backoff *= 1.7
        raise ChatAPIError("Не удалось получить ответ за отведённое число попыток",
                           kind="rate")

    async def chat_once(
        self,
        messages: list,
        model: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: Optional[int] = 1000,
    ) -> str:
        buf = []
        async for piece in self.chat_stream(messages, model, temperature, max_tokens):
            buf.append(piece)
        return "".join(buf).strip()

    # ------------------------------------------------------------------ аудио

    async def transcribe(self, data: bytes, filename: str = "sound.ogg",
                         mime: str = "audio/ogg") -> str:
        r = await self.client.post(
            "/audio/transcriptions",
            data={"model": TRANSCRIBE_MODEL, "response_format": "text"},
            files={"file": (filename, data, mime)},
            timeout=httpx.Timeout(30.0, read=180.0),
        )
        if r.status_code != 200:
            raise ChatAPIError(
                f"Не удалось расшифровать аудио (HTTP {r.status_code}): {r.text[:300]}")
        return r.text.strip()


groq = GroqClient()
