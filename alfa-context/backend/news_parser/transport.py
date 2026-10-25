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
ALFA_TLS_HOSTS = {'alfabank.ru', 'www.alfabank.ru'}


def alfa_browser_host(host):
    host = host.lower().lstrip('.')
    return (host == 'alfabank.ru' or host.endswith('.alfabank.ru')
            or host == 'alfabank.servicecdn.ru')


def browser_state_for(saved, *, alfa):
    """Keep each source's first-party session in its own browser context."""
    return {
        'cookies': [cookie for cookie in saved.get('cookies', [])
                    if alfa_browser_host(cookie.get('domain', '')) == alfa],
        'origins': [origin for origin in saved.get('origins', [])
                    if alfa_browser_host(urlsplit(origin.get('origin', '')).hostname or '') == alfa],
    }


class FetchError(RuntimeError):
    pass


class Transport:
    def __init__(self, settings: Settings, client=None, *, alfa_client=None):
        self.settings = settings
        options = dict(timeout=settings.timeout, follow_redirects=False,
                       headers={"User-Agent": USER_AGENT, "Accept-Language": "ru,en;q=0.5"})
        self.client = client or httpx.AsyncClient(**options)
        # The user explicitly requested this exception for the Alfa source.
        # This client is never used for another host or a cross-source redirect.
        self._alfa_client = alfa_client or client or httpx.AsyncClient(verify=False, **options)
        self._locks = defaultdict(asyncio.Lock)
        self._last = defaultdict(float)
        self._browser_lock = asyncio.Lock()
        self._pw = self._browser = self._context = None
        self._alfa_context = None
        self._context_options = {}
        self._verified = set()
        self._session_metadata = {}
        self._saved_browser_state = {'cookies': [], 'origins': []}
        if settings.browser_state and Path(settings.browser_state).exists():
            saved = json.loads(Path(settings.browser_state).read_text(encoding='utf-8'))
            self._saved_browser_state = {key: saved.get(key, []) for key in ('cookies', 'origins')}
            self._session_metadata = saved.get('_news_parser', {})
            if self._session_metadata.get('user_agent'):
                self.client.headers['User-Agent'] = self._session_metadata['user_agent']
                self._alfa_client.headers['User-Agent'] = self._session_metadata['user_agent']
            for cookie in saved.get('cookies', []):
                if cookie.get('domain', '').lstrip('.') in HOSTS:
                    target = self._http_client('https://' + cookie['domain'].lstrip('.'))
                    target.cookies.set(cookie['name'], cookie['value'],
                                       domain=cookie['domain'], path=cookie.get('path', '/'))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        try:
            if self._context:
                if self.settings.browser_state and self._verified:
                    from .storage import atomic_json
                    path = Path(self.settings.browser_state)
                    strict = browser_state_for(await self._context.storage_state(), alfa=False)
                    alfa = browser_state_for(
                        await self._alfa_context.storage_state() if self._alfa_context else self._saved_browser_state,
                        alfa=True)
                    saved = {key: strict[key] + alfa[key] for key in ('cookies', 'origins')}
                    saved['_news_parser'] = {'user_agent': self.client.headers['User-Agent']}
                    atomic_json(path, saved)
                    if os.name != 'nt':
                        path.chmod(0o600)
                await self._context.close()
            if self._alfa_context:
                await self._alfa_context.close()
            if self._browser:
                await self._browser.close()
        finally:
            if self._pw:
                await self._pw.stop()
            await self.client.aclose()
            if self._alfa_client is not self.client:
                await self._alfa_client.aclose()

    def _http_client(self, url):
        return self._alfa_client if urlsplit(url).hostname in ALFA_TLS_HOSTS else self.client

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
                    async with self._http_client(current).stream("GET", current) as response:
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
                version = self._browser.version
                ua = self._session_metadata.get('user_agent') or f'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version} Safari/537.36'
                self._context_options = {'locale': 'ru-RU', 'timezone_id': 'Europe/Moscow',
                                         'user_agent': ua, 'viewport': {'width': 1365, 'height': 900}}
                self._context = await self._browser.new_context(
                    **self._context_options, storage_state=browser_state_for(self._saved_browser_state, alfa=False))
                self.client.headers['User-Agent'] = ua
                self._alfa_client.headers['User-Agent'] = ua
                await self._configure_context(self._context)
            except Exception as exc:
                if self._pw:
                    await self._pw.stop()
                self._pw = self._browser = self._context = None
                raise FetchError(f"Chromium unavailable: {exc}") from exc

    async def _configure_context(self, context, *, alfa=False):
        context.set_default_timeout(self.settings.timeout * 1000)

        async def route(request_route):
            request = request_route.request
            host = urlsplit(request.url).hostname or ''
            allowed = browser_request_allowed(request.url, request.resource_type)
            if alfa:
                allowed = allowed and (alfa_browser_host(host) or host == 'servicepipe.tech'
                                       or host.endswith('.servicepipe.tech'))
                if request.is_navigation_request() and request.frame.parent_frame is None:
                    allowed = allowed and host in ALFA_TLS_HOSTS
            if allowed:
                await request_route.continue_()
            else:
                await request_route.abort()
        await context.route('**/*', route)

    async def _browser_context(self, url):
        url = request_url(url)
        await self._ensure_browser()
        if urlsplit(url).hostname not in ALFA_TLS_HOSTS:
            return self._context
        async with self._browser_lock:
            if self._alfa_context is None:
                self._alfa_context = await self._browser.new_context(
                    **self._context_options, ignore_https_errors=True,
                    storage_state=browser_state_for(self._saved_browser_state, alfa=True))
                await self._configure_context(self._alfa_context, alfa=True)
            return self._alfa_context

    async def _open(self, url):
        url = request_url(url)
        context = await self._browser_context(url)
        await self._pace(url)
        page = await context.new_page()
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
                pass
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
            for cookie in await context.cookies([url]):
                self._http_client(url).cookies.set(cookie['name'], cookie['value'],
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

    async def _alfa_snapshot(self, page, url, now, *, previous=None):
        """Wait for an actual, stable release list after React pagination."""
        from .alfa_extract import alfa_listing_candidates, alfa_url
        deadline = time.monotonic() + self.settings.browser_timeout
        fingerprint, stable = None, 0
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(page.content(), timeout=min(
                    5, max(0.05, deadline - time.monotonic())))
                if is_denied(raw):
                    raise FetchError(f'Alfa denied access: {url}')
                if not is_challenge(raw):
                    alfa_url(page.url, url, listing=True)
                    items = alfa_listing_candidates(raw, page.url, now=now)
                    current = tuple(item.url for item in items)
                    if current and current != previous:
                        stable = stable + 1 if current == fingerprint else 1
                        fingerprint = current
                        if stable >= 2:
                            return raw, items
            except FetchError:
                raise
            except Exception as exc:
                message = str(exc).lower()
                if not isinstance(exc, asyncio.TimeoutError) and not any(
                        marker in message for marker in ('navigat', 'execution context',
                                                         'changing the content')):
                    raise
                stable = 0
            await page.wait_for_timeout(250)
        raise FetchError('Alfa release list did not change or settle within '
                         f'{self.settings.browser_timeout:g}s: {url}')

    async def _alfa_next_control(self, page, page_number):
        """The site's pagination uses numbered buttons, not link URLs."""
        next_controls, numbered_controls = [], []
        for control in await page.locator('button, a[rel="next"]').all():
            if not await control.is_visible() or not await control.is_enabled():
                continue
            if (await control.get_attribute('aria-disabled') or '').lower() == 'true':
                continue
            text = (await control.inner_text() or '').strip()
            aria = (await control.get_attribute('aria-label') or '').strip()
            labels = (text, aria)
            if any(label == str(page_number) or re.fullmatch(
                    rf'(?:страница|page)\s*{page_number}', label, re.I)
                    for label in labels):
                numbered_controls.append(control)
            if (await control.get_attribute('rel') == 'next' or any(
                    re.search(r'следующ|далее|показать\s+ещ[её]|загрузить\s+ещ[её]|\bnext\b',
                              label, re.I) or label in {'›', '»', '>', '>>'}
                    for label in labels)):
                next_controls.append(control)
        # Numbered buttons match the actual current Alfa markup. Avoid a generic
        # "show more" control elsewhere on the page when that button is present.
        return next(iter(numbered_controls or next_controls), None)

    async def alfa_pages(self, url, cutoff, now):
        """Yield one HTML page at a time; the caller can stop at its own limit."""
        from datetime import timedelta
        from .alfa_extract import alfa_next_page, alfa_url
        url = alfa_url(url, url, listing=True)
        page = await self._open(url)
        previous, visited = None, set()
        try:
            for index in range(self.settings.max_pages):
                raw, items = await self._alfa_snapshot(page, url, now, previous=previous)
                fingerprint = tuple(item.url for item in items)
                if fingerprint in visited:
                    raise FetchError('Alfa pagination repeated an earlier release list')
                visited.add(fingerprint)
                yield raw
                # Day-only card dates cannot reject the cutoff day's articles.
                if index + 1 == self.settings.max_pages or all(
                        item.published_at and item.published_at + timedelta(days=1) <= cutoff
                        for item in items):
                    break
                following = alfa_next_page(raw, page.url)
                if following:
                    await self._pace(following)
                    await page.goto(following, wait_until='domcontentloaded')
                else:
                    control = await self._alfa_next_control(page, index + 2)
                    if control is None:
                        break
                    await self._pace(url)
                    await control.click()
                previous = fingerprint
        except Exception as exc:
            raise FetchError(f'Alfa browser pagination failed: {exc}') from exc
        finally:
            await page.close()

    async def _tass_snapshot(self, page, url, *, article=False):
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
                        parsed = article_from_html(raw, 'tass', Candidate(url), rendered=True)
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
        from .extract import listing_candidates
        from .tass_scroll import FEED_ROOT, FEED_CARDS, FEED_LOADER, SCROLL_FEED, FEED_GREW
        from playwright.async_api import TimeoutError as BrowserTimeoutError
        page = await self._tass_page(url)
        pages, ids = [], set()
        selectors = dict(rootSelector=FEED_ROOT, cardSelector=FEED_CARDS,
                         loaderSelector=FEED_LOADER)
        load_timeout = min(self.settings.browser_timeout, self.settings.timeout) * 1000
        try:
            for index in range(self.settings.max_pages):
                try:
                    raw = await self._tass_snapshot(page, url)
                except Exception:
                    if pages:
                        log.warning('TASS list stopped loading; keeping %d discovered links', len(ids))
                        break
                    raise
                pages.append(raw)
                ids.update(c.url for c in listing_candidates(raw, url, 'tass'))
                log.info('TASS feed: %d unique links after batch %d', len(ids), index + 1)
                if len(ids) >= target or index + 1 == self.settings.max_pages:
                    break
                grew = False
                for attempt in range(2):
                    try:
                        if attempt:
                            await page.evaluate(SCROLL_FEED, {**selectors, 'reset': True})
                            await page.wait_for_timeout(100)
                        if not await page.evaluate(SCROLL_FEED, {**selectors, 'reset': False}):
                            break
                        handle = await page.wait_for_function(
                            FEED_GREW,
                            arg=dict(rootSelector=FEED_ROOT, cardSelector=FEED_CARDS,
                                     known=list(ids)), timeout=load_timeout, polling=100)
                        await handle.dispose()
                        grew = True
                        break
                    except BrowserTimeoutError:
                        continue
                    except Exception:
                        log.warning('TASS feed navigation failed; keeping %d discovered links', len(ids))
                        break
                if not grew:
                    log.info('TASS feed has no new links after bounded waits; keeping %d links', len(ids))
                    break
            return pages
        finally:
            await page.close()

    async def listing(self, url, source, target, cutoff):
        from .extract import listing_candidates
        pages, ids, stale = [], set(), 0
        page = await self._open(url)
        try:
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
