import argparse
import asyncio
import os
from pathlib import Path

from .access import has_source_content
from .models import Settings
from .storage import atomic_json
from .transport import Transport, FetchError

URLS = {'tass': 'https://tass.ru/ekonomika'}


async def prepare(args):
    transport = Transport(Settings(browser_state=args.output))
    from playwright.async_api import async_playwright
    async with transport, async_playwright() as pw:
        from urllib.parse import unquote, urlsplit
        launch = {'headless': False}
        if args.channel:
            launch['channel'] = args.channel
        value = os.getenv('HTTPS_PROXY') or os.getenv('HTTP_PROXY') or os.getenv('ALL_PROXY')
        if value:
            proxy = urlsplit(value)
            launch['proxy'] = {'server': f'{proxy.scheme}://{proxy.hostname}:{proxy.port or 80}'}
            if proxy.username:
                launch['proxy'].update(username=unquote(proxy.username), password=unquote(proxy.password or ''))
        browser = await pw.chromium.launch(**launch)
        options = {'locale': 'ru-RU', 'timezone_id': 'Europe/Moscow'}
        if Path(args.output).exists():
            import json
            saved = json.loads(Path(args.output).read_text(encoding='utf-8'))
            options['storage_state'] = {key: saved.get(key, []) for key in ('cookies', 'origins')}
        context = await browser.new_context(**options)
        try:
            for source in args.sources:
                page = await context.new_page()
                try:
                    await page.goto(URLS[source], wait_until='domcontentloaded', timeout=30000)
                except Exception as exc:
                    print(f'{source}: navigation: {exc}')
                print(f'{source}: дождитесь новостей в браузере. Если показана CAPTCHA, пройдите её вручную.')
                await asyncio.to_thread(input, 'Когда нужная страница загрузится, нажмите Enter здесь: ')
                raw = await page.content()
                if not has_source_content(raw, URLS[source]):
                    raise FetchError(f'{source}: actual source content is still unavailable; session was not saved')
            path = Path(args.output)
            saved = await context.storage_state()
            saved['_news_parser'] = {'user_agent': await page.evaluate('navigator.userAgent')}
            atomic_json(path, saved)
            if os.name != 'nt':
                path.chmod(0o600)
            print(f'Сессия сохранена: {path}. Запускайте Docker из той же сети.')
        finally:
            await context.close()
            await browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='../data/news/.browser-state.json')
    parser.add_argument('--sources', nargs='+', choices=list(URLS), default=list(URLS))
    parser.add_argument('--channel', choices=['chrome', 'msedge'], help='Use an installed desktop browser instead of bundled Chromium')
    args = parser.parse_args()
    try:
        asyncio.run(prepare(args))
    except (RuntimeError, OSError, ValueError) as exc:
        parser.exit(1, f'{exc}\n')


if __name__ == '__main__':
    main()
