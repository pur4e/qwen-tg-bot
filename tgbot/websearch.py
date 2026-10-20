"""Веб-поиск без API-ключей: библиотека ddgs с фолбэком на DuckDuckGo HTML."""
import asyncio
import html as html_mod
import logging
import re
from typing import Optional

import httpx

log = logging.getLogger("search")

try:
    from ddgs import DDGS
except ImportError:  # старое имя пакета
    try:
        from duckduckgo_search import DDGS  # type: ignore
    except ImportError:
        DDGS = None  # type: ignore

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


async def search_web(query: str, n: int = 5) -> list:
    """Возвращает список [{title, url, snippet}], пустой список если ничего не нашлось."""
    results = []
    query = (query or "").strip()
    if not query:
        return results
    if DDGS is not None:
        try:
            raw = await asyncio.to_thread(
                lambda: DDGS().text(query, max_results=n, region="ru-ru")
            )
            for r in raw or []:
                results.append({
                    "title": (r.get("title") or "")[:120],
                    "url": r.get("href") or r.get("url") or "",
                    "snippet": (r.get("body") or r.get("snippet") or "")[:350],
                })
        except Exception as e:
            log.warning("DDGS упал (%s), переключаюсь на HTML-фолбэк", e)
            results = []
    if not results:
        results = await _scrape_duck(query, n)
    return [r for r in results if r.get("title") and r.get("url")]


async def _scrape_duck(query: str, n: int) -> list:
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": UA}, timeout=20, follow_redirects=True
        ) as client:
            r = await client.get("https://html.duckduckgo.com/html/", params={"q": query})
        links = re.findall(
            r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            r.text, re.S,
        )
        snips = re.findall(
            r'class="result__snippet"[^>]*>(.*?)</(?:a|div)>',
            r.text, re.S,
        )
        out = []
        for i, (url, title) in enumerate(links[:n]):
            snip = snips[i] if i < len(snips) else ""
            out.append({
                "title": _clean(title)[:120],
                "url": html_mod.unescape(url),
                "snippet": _clean(snip)[:350],
            })
        return out
    except Exception as e:
        log.warning("HTML-фолбэк поиска тоже упал: %s", e)
        return []


def _clean(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s or "")
    return html_mod.unescape(s).strip()


async def fetch_page(url: str, limit: int = 4000) -> str:
    """Скачивает страницу и вычищает из неё текст."""
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": UA}, timeout=25, follow_redirects=True
        ) as client:
            r = await client.get(url)
        text = r.text
    except Exception as e:
        return f"(страница недоступна: {e})"
    text = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    text = re.sub(r"[ \t\r\f]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text[:limit]


def format_results(results: list, query: str) -> str:
    lines = [
        "🌐 Ниже — свежие результаты веб-поиска по запросу пользователя. "
        "Используй их при ответе, если они уместны, и не выдавай свои старые знания за новейшие.",
        f"Запрос: «{query}»",
    ]
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] {r['title']}\n    {r['url']}\n    {r['snippet']}")
    return "\n\n".join(lines)
