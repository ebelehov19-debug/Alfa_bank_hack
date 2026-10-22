from __future__ import annotations

import asyncio
from collections import defaultdict
import logging
import os
import re
import time
from urllib.parse import unquote, urlsplit
from pathlib import Path
import json

import httpx

from .access import browser_request_allowed, has_source_content, is_challenge, is_denied
from .models import HOSTS, Settings, request_url

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
        self._verified = set()
        self._session_metadata = {}
        if settings.browser_state and Path(settings.browser_state).exists():
            saved = json.loads(Path(settings.browser_state).read_text(encoding='utf-8'))
            self._session_metadata = saved.get('_news_parser', {})
            if self._session_metadata.get('user_agent'):
                self.client.headers['User-Agent'] = self._session_metadata['user_agent']
            for cookie in saved.get('cookies', []):
                if cookie.get('domain', '').lstrip('.') in HOSTS:
                    self.client.cookies.set(cookie['name'], cookie['value'],
                                            domain=cookie['domain'], path=cookie.get('path', '/'))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        try:
            if self._context:
                if self.settings.browser_state and self._verified:
                    from .storage import atomic_json
                    path = Path(self.settings.browser_state)
                    saved = await self._context.storage_state()
                    saved['_news_parser'] = {'user_agent': self.client.headers['User-Agent']}
                    atomic_json(path, saved)
                    if os.name != 'nt':
                        path.chmod(0o600)
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
        url = request_url(url)
        original_host = urlsplit(url).hostname.removeprefix('www.')
        last = None
        for attempt in range(self.settings.retries + 1):
            current = url
            try:
                for _ in range(6):
                    await self._pace(current)
                    async with self.client.stream("GET", current) as response:
                        if response.is_redirect:
                            current = request_url(str(response.url.join(response.headers["location"])))
                            if urlsplit(current).hostname.removeprefix('www.') != original_host:
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
                        raw = b"".join(parts).decode(encoding, errors="replace")
                        if is_challenge(raw):
                            raise FetchError(f'JavaScript access check at {url}; browser session required')
                        return raw
                raise FetchError("Too many redirects")
            except httpx.HTTPStatusError as exc:
                last = exc
                # CAPTCHA/403 may be rendered by the browser; don't hammer HTTP.
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
                # Match the actual installed browser version. Its default headless
                # UA was rejected by these sources before their visitor check ran.
                version = self._browser.version
                ua = self._session_metadata.get('user_agent') or f'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version} Safari/537.36'
                context_options = {'locale': 'ru-RU', 'timezone_id': 'Europe/Moscow',
                                   'user_agent': ua, 'viewport': {'width': 1365, 'height': 900}}
                if self.settings.browser_state and Path(self.settings.browser_state).exists():
                    saved = json.loads(Path(self.settings.browser_state).read_text(encoding='utf-8'))
                    context_options['storage_state'] = {key: saved.get(key, []) for key in ('cookies', 'origins')}
                self._context = await self._browser.new_context(**context_options)
                self.client.headers['User-Agent'] = ua
                self._context.set_default_timeout(self.settings.timeout * 1000)

                async def route(request_route):
                    request = request_route.request
                    # Pages may need official static resources to execute JavaScript.
                    # Both sources use Servicepipe for their ordinary visitor check.
                    # Blocking its JS prevented Chromium from ever loading the site.
                    if not browser_request_allowed(request.url, request.resource_type):
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
        url = request_url(url)
        await self._ensure_browser()
        await self._pace(url)
        page = await self._context.new_page()
        status = [None]
        def navigation_response(response):
            if response.request.is_navigation_request() and response.frame == page.main_frame:
                status[0] = response.status
        page.on('response', navigation_response)
        try:
            from playwright.async_api import TimeoutError as BrowserTimeoutError
            try:
                await page.goto(url, wait_until="domcontentloaded")
            except BrowserTimeoutError:
                # JS visitor checks can navigate before DOMContentLoaded arrives.
                # The bounded readiness loop below decides whether content loaded.
                pass
            # A 403/503 with a JS check can navigate to a real 200 document after
            # scripts finish. Wait for that navigation, not a fixed one-second sleep.
            deadline = time.monotonic() + self.settings.browser_timeout
            while True:
                try:
                    raw = await asyncio.wait_for(page.content(), timeout=min(10, self.settings.browser_timeout))
                    checking = is_challenge(raw)
                    if not checking:
                        if is_denied(raw) or (status[0] and status[0] >= 400):
                            raise FetchError(f'Chromium access denied (HTTP {status[0] or "unknown"}): {url}')
                        request_url(page.url)
                        if has_source_content(raw, url):
                            break
                except asyncio.TimeoutError:
                    raise FetchError(f'Browser page remained busy during its visitor check: {url}') from None
                except FetchError:
                    raise
                except Exception as exc:
                    if 'navigation' not in str(exc).lower() and 'execution context' not in str(exc).lower():
                        raise
                if time.monotonic() >= deadline:
                    raise FetchError(f'Visitor check/CAPTCHA did not complete within {self.settings.browser_timeout:g}s: {url}. Use a verified --browser-state or a network accepted by the source')
                await page.wait_for_timeout(500)
            for cookie in await self._context.cookies([url]):
                self.client.cookies.set(cookie['name'], cookie['value'],
                                        domain=cookie['domain'], path=cookie['path'])
            self._verified.add(urlsplit(url).hostname)
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

    async def _tass_snapshot(self, page, url, *, article=False):
        """TASS-only bounded DOM reads, including reads after redirects.

        Do not change e-disclosure navigation while that adapter is out of scope.
        """
        from .extract import article_from_html, listing_candidates
        from .models import Candidate
        deadline = time.monotonic() + self.settings.browser_timeout
        last_error, previous, stable = None, None, 0
        while time.monotonic() < deadline:
            try:
                remaining = max(0.05, deadline - time.monotonic())
                raw = await asyncio.wait_for(page.content(), min(remaining, 5))
                if is_denied(raw):
                    raise FetchError(f'TASS denied access: {url}')
                if not is_challenge(raw) and has_source_content(raw, url):
                    if article:
                        parsed = article_from_html(raw, 'tass', Candidate(url))
                        ready = bool(parsed.title and len(parsed.text) >= 40)
                        fingerprint = (parsed.title, parsed.text, parsed.published_at)
                    else:
                        items = listing_candidates(raw, url, 'tass')
                        ready = bool(items)
                        fingerprint = tuple(c.url for c in items)
                    if ready:
                        stable = stable + 1 if fingerprint == previous else 1
                        previous = fingerprint
                        if stable >= 2:
                            request_url(page.url)
                            for cookie in await self._context.cookies([url]):
                                self.client.cookies.set(cookie['name'], cookie['value'],
                                                        domain=cookie['domain'], path=cookie['path'])
                            self._verified.add(urlsplit(url).hostname)
                            return raw
            except FetchError:
                raise
            except Exception as exc:
                message = str(exc).lower()
                if not isinstance(exc, asyncio.TimeoutError) and not any(
                        marker in message for marker in ('navigat', 'execution context', 'changing the content')):
                    raise
                last_error = type(exc).__name__
                stable = 0
            await page.wait_for_timeout(250)
        raise FetchError(f'TASS page did not settle within {self.settings.browser_timeout:g}s: {url}'
                         + (f' ({last_error})' if last_error else ''))

    async def _tass_page(self, url):
        from playwright.async_api import TimeoutError as BrowserTimeoutError
        url = request_url(url)
        if urlsplit(url).hostname not in {'tass.ru', 'www.tass.ru'}:
            raise ValueError('TASS browser adapter only accepts tass.ru')
        await self._ensure_browser()
        await self._pace(url)
        page = await self._context.new_page()
        try:
            try:
                await page.goto(url, wait_until='domcontentloaded')
            except BrowserTimeoutError:
                pass
            return page
        except Exception:
            await page.close()
            raise

    async def tass_render(self, url):
        page = await self._tass_page(url)
        try:
            return await self._tass_snapshot(page, url, article=True)
        finally:
            await page.close()

    async def tass_listing(self, url, source, target, cutoff):
        """Keep loaded list pages; later navigation failure cannot discard them."""
        from .extract import listing_candidates
        page = await self._tass_page(url)
        pages, ids, stale = [], set(), 0
        try:
            for _ in range(self.settings.max_pages):
                try:
                    raw = await self._tass_snapshot(page, url)
                except Exception:
                    if pages:
                        log.warning('TASS list stopped loading; keeping %d discovered links', len(ids))
                        break
                    raise
                pages.append(raw)
                before = len(ids)
                ids.update(c.url for c in listing_candidates(raw, url, 'tass'))
                stale = stale + 1 if before == len(ids) else 0
                if len(ids) >= target or stale >= 2:
                    break
                import re as regex
                buttons = page.get_by_role('button', name=regex.compile(r'^(?:Показать|Загрузить)\s+ещ[её]$', regex.I))
                clicked = False
                for button in await buttons.all():
                    if await button.is_visible() and await button.is_enabled():
                        await button.click()
                        clicked = True
                        break
                if not clicked:
                    await page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                await page.wait_for_timeout(max(750, self.settings.delay * 1000))
            return pages
        finally:
            await page.close()

    async def listing(self, url, source, target, cutoff):
        """Render public lists, interact with date search and load-more controls.

        Return all observed HTML pages, including replaced pagination pages.
        Unknown UI or CAPTCHA results in a partial/error report, not an empty success.
        """
        from .extract import listing_candidates
        pages, ids, stale = [], set(), 0
        page = await self._open(url)
        try:
            if source == "e_disclosure":
                # Use the site's own search form; do not invent a private API URL.
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
                    # The date range is selected in a modal on the legacy portal.
                    confirm = page.get_by_role('button', name='Подтвердить выбор', exact=True)
                    for item in await confirm.all():
                        if await item.is_visible():
                            await item.click()
                            break
                # Several modal dialogs also have 'Искать'; select a visible search action.
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
                # Explicit pagination is preferable to infinite scroll when available.
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
