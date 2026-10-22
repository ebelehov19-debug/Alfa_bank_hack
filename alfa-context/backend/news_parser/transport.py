from __future__ import annotations

import asyncio
from collections import defaultdict
import logging
import os
import re
import time
from urllib.parse import unquote, urlsplit

import httpx

from .models import HOSTS, Settings, canonical_url

log = logging.getLogger(__name__)
USER_AGENT = "AlfaHackNewsParser/0.1 (+public-news-research)"


class FetchError(RuntimeError):
    pass


class Transport:
    """Bounded HTTP requests and a lazily started, shared Chromium instance."""

    def __init__(self, settings: Settings, client=None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(
            timeout=settings.timeout, follow_redirects=False,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "ru,en;q=0.5"},
        )
        self._locks = defaultdict(asyncio.Lock)
        self._last = defaultdict(float)
        self._browser_lock = asyncio.Lock()
        self._pw = self._browser = self._context = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        try:
            if self._context:
                await self._context.close()
            if self._browser:
                await self._browser.close()
        finally:
            if self._pw:
                await self._pw.stop()
            await self.client.aclose()

    async def _pace(self, url):
        host = (urlsplit(url).hostname or "").removeprefix("www.")
        async with self._locks[host]:
            delay = self.settings.delay - (time.monotonic() - self._last[host])
            if delay > 0:
                await asyncio.sleep(delay)
            self._last[host] = time.monotonic()

    async def get(self, url: str) -> str:
        url = canonical_url(url)
        original_host = urlsplit(url).hostname
        last = None
        for attempt in range(self.settings.retries + 1):
            current = url
            try:
                for _ in range(6):
                    await self._pace(current)
                    async with self.client.stream("GET", current) as response:
                        if response.is_redirect:
                            current = canonical_url(str(response.url.join(response.headers["location"])))
                            if urlsplit(current).hostname != original_host:
                                raise FetchError("Redirect to another source is not allowed")
                            continue
                        response.raise_for_status()
                        parts, size = [], 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > 8 * 1024 * 1024:
                                raise FetchError("Response exceeds 8 MiB")
                            parts.append(chunk)
                        encoding = response.encoding or "utf-8"
                        return b"".join(parts).decode(encoding, errors="replace")
                raise FetchError("Too many redirects")
            except httpx.HTTPStatusError as exc:
                last = exc

                if exc.response.status_code not in {408, 429, 500, 502, 503, 504}:
                    break
                retry_after = exc.response.headers.get("retry-after", "")
                backoff = min(float(retry_after), 30) if retry_after.isdigit() else min(2 ** attempt, 10)
            except httpx.TransportError as exc:
                last, backoff = exc, min(2 ** attempt, 10)
            if attempt < self.settings.retries:
                await asyncio.sleep(backoff)
        raise FetchError(f"GET {url}: {last}")

    async def _ensure_browser(self):
        if not self.settings.browser:
            raise FetchError("Chromium fallback disabled (--no-browser)")
        async with self._browser_lock:
            if self._context:
                return
            try:
                from playwright.async_api import async_playwright
                self._pw = await async_playwright().start()
                launch = {"headless": True}
                proxy_url = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or os.getenv("ALL_PROXY")
                if proxy_url:
                    proxy = urlsplit(proxy_url)
                    launch['proxy'] = {"server": f"{proxy.scheme}://{proxy.hostname}:{proxy.port or 80}"}
                    if proxy.username:
                        launch['proxy']['username'] = unquote(proxy.username)
                        launch['proxy']['password'] = unquote(proxy.password or '')
                self._browser = await self._pw.chromium.launch(**launch)
                self._context = await self._browser.new_context(locale="ru-RU", timezone_id="Europe/Moscow")
                self._context.set_default_timeout(self.settings.timeout * 1000)

                async def route(request_route):
                    request = request_route.request
                    host = urlsplit(request.url).hostname or ""
                    allowed = host in HOSTS or host.endswith((".tass.ru", ".moex.com", ".e-disclosure.ru"))
                    if request.resource_type in {"image", "media", "font"} or not allowed:
                        await request_route.abort()
                    else:
                        await request_route.continue_()
                await self._context.route("**/*", route)
            except Exception as exc:
                if self._pw:
                    await self._pw.stop()
                self._pw = self._browser = self._context = None
                raise FetchError(f"Chromium unavailable: {exc}") from exc

    async def _open(self, url):
        url = canonical_url(url)
        await self._ensure_browser()
        await self._pace(url)
        page = await self._context.new_page()
        try:
            response = await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_timeout(1000)
            if response and response.status >= 400:
                raise FetchError(f"Chromium HTTP {response.status}: {url}")
            canonical_url(page.url)
            return page
        except Exception:
            await page.close()
            raise

    async def render(self, url):
        try:
            page = await self._open(url)
            try:
                return await page.content()
            finally:
                await page.close()
        except Exception as exc:
            raise FetchError(f"Browser rendering failed: {exc}") from exc

    async def listing(self, url, source, target, cutoff):
        from .extract import listing_candidates
        pages, ids, stale = [], set(), 0
        page = await self._open(url)
        try:
            if source == "e_disclosure":
                date_picker = page.get_by_text('Выбрать дату публикации', exact=False)
                if await date_picker.count() and await date_picker.first.is_visible():
                    await date_picker.first.click()
                inputs = page.locator('input:not([type="hidden"]):not([type="checkbox"]):not([type="radio"])')
                dated = []
                for i in range(await inputs.count()):
                    item = inputs.nth(i)
                    props = " ".join([(await item.get_attribute(a)) or "" for a in ("id", "name", "class", "placeholder")])
                    if await item.is_visible() and any(s in props.lower() for s in ("date", "дд.мм", "дата")):
                        dated.append(item)
                if len(dated) >= 2:
                    from .models import MSK
                    from datetime import datetime
                    for item, date in zip(dated[:2], (cutoff.astimezone(MSK), datetime.now(MSK))):
                        value = date.strftime("%Y-%m-%d") if await item.get_attribute("type") == "date" else date.strftime("%d.%m.%Y")
                        await item.fill(value)
                    confirm = page.get_by_role('button', name='Подтвердить выбор', exact=True)
                    for item in await confirm.all():
                        if await item.is_visible():
                            await item.click()
                            break
                for control in await page.locator('button, input[type="submit"], input[type="button"], a.btn').all():
                    label = (await control.inner_text()) or (await control.get_attribute("value")) or ""
                    if await control.is_visible() and label.strip() in {"Искать", "Поиск", "Найти"}:
                        await control.click()
                        await page.wait_for_timeout(1500)
                        break
            for page_index in range(self.settings.max_pages):
                raw = await page.content()
                pages.append(raw)
                items = listing_candidates(raw, page.url, source)
                old = len(ids)
                ids.update(c.url for c in items)
                stale = stale + 1 if len(ids) == old else 0
                if len(ids) >= target or stale >= 3:
                    break
                clicked = False
                for control in await page.locator('a, button, input[type="button"], input[type="submit"]').all():
                    label = ((await control.inner_text()) or (await control.get_attribute("value")) or "").strip()
                    rel = await control.get_attribute("rel")
                    title = (await control.get_attribute('title')) or ''
                    if (rel == "next" or 'следующ' in title.lower() or label in {"Показать еще", "Показать ещё", "Загрузить еще", "Загрузить ещё", "Следующая", "Далее", "›", "»", ">", ">>"}) and await control.is_visible() and await control.is_enabled():
                        await control.click()
                        clicked = True
                        break
                if not clicked:
                    numeric = page.locator('.pagination a, .pager a, .paging a, .navs a').filter(
                        has_text=re.compile(rf'^\s*{page_index + 2}\s*$'))
                    for control in await numeric.all():
                        if await control.is_visible() and await control.is_enabled():
                            await control.click()
                            clicked = True
                            break
                if not clicked:
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(max(1000, self.settings.delay * 1000))
            return pages
        except Exception as exc:
            raise FetchError(f"Browser list failed: {exc}") from exc
        finally:
            await page.close()
