import argparse
import asyncio
import json
import os
from pathlib import Path

from .access import has_source_content
from .models import Settings
from .storage import atomic_json
from .transport import Transport, FetchError, browser_state_for

URLS = {'tass': 'https://tass.ru/ekonomika',
        'alfa': 'https://alfabank.ru/news/t/release/'}


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
        saved = {}
        if Path(args.output).exists():
            saved = json.loads(Path(args.output).read_text(encoding='utf-8'))
        contexts = {}
        user_agent = saved.get('_news_parser', {}).get('user_agent')
        try:
            for source in args.sources:
                alfa = source == 'alfa'
                if alfa not in contexts:
                    options = {'locale': 'ru-RU', 'timezone_id': 'Europe/Moscow',
                               'ignore_https_errors': alfa,
                               'storage_state': browser_state_for(saved, alfa=alfa)}
                    if user_agent:
                        options['user_agent'] = user_agent
                    context = await browser.new_context(**options)
                    contexts[alfa] = context
                    await transport._configure_context(context, alfa=alfa)
                context = contexts[alfa]
                page = await context.new_page()
                try:
                    try:
                        await page.goto(URLS[source], wait_until='domcontentloaded', timeout=30000)
                    except Exception as exc:
                        print(f'{source}: navigation: {exc}')
                    print(f'{source}: дождитесь новостей в браузере. Если показана CAPTCHA, пройдите её вручную.')
                    await asyncio.to_thread(input, 'Когда нужная страница загрузится, нажмите Enter здесь: ')
                    raw = await page.content()
                    if not has_source_content(raw, URLS[source]):
                        raise FetchError(f'{source}: actual source content is still unavailable; session was not saved')
                    if not user_agent:
                        user_agent = await page.evaluate('navigator.userAgent')
                finally:
                    await page.close()
            path = Path(args.output)
            merged = {'cookies': [], 'origins': []}
            for alfa in (False, True):
                state = (await contexts[alfa].storage_state()
                         if alfa in contexts else saved)
                partition = browser_state_for(state, alfa=alfa)
                for key in merged:
                    merged[key].extend(partition[key])
            merged['_news_parser'] = {'user_agent': user_agent}
            atomic_json(path, merged)
            if os.name != 'nt':
                path.chmod(0o600)
            print(f'Сессия сохранена: {path}. Запускайте Docker из той же сети.')
        finally:
            try:
                for context in contexts.values():
                    await context.close()
            finally:
                await browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='../data/news/.browser-state.json')
    parser.add_argument('--sources', nargs='+', choices=list(URLS), default=['tass'])
    parser.add_argument('--channel', choices=['chrome', 'msedge'], help='Use an installed desktop browser instead of bundled Chromium')
    args = parser.parse_args()
    try:
        asyncio.run(prepare(args))
    except (RuntimeError, OSError, ValueError) as exc:
        parser.exit(1, f'{exc}\n')


if __name__ == '__main__':
    main()
