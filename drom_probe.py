"""
Проверка каталога drom.ru: как устроены страницы рынка (Япония / Корея / Китай),
поколения (группы комплектаций с мощностью, объёмом, КПП, приводом, кузовом)
и одной комплектации. Всё нужное печатается в лог; HTML — в drom_debug/
(артефакт «drom-probe»). Ничего не отправляет на сайт.
"""

import json
import os
import re
import time
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "drom_debug")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")
MARKETS = [
    "https://www.drom.ru/catalog/honda/fit/japan/",
    "https://www.drom.ru/catalog/hyundai/avante/south-korea/",
    "https://www.drom.ru/catalog/geely/monjaro/china/",
]


def save(name, content):
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, name), "w", encoding="utf-8") as f:
        f.write(content)


def around(html, needle, width=1500):
    i = html.find(needle)
    return html[max(0, i - width // 3): i + width] if i >= 0 else ""


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        page = browser.new_context(user_agent=UA, locale="ru-RU", timezone_id="Asia/Vladivostok",
                                   extra_http_headers={"Accept-Language": "ru-RU,ru;q=0.9"}).new_page()

        def open_(url, name):
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(2500)
            html = page.content()
            save(name + ".html", html)
            time.sleep(3)
            return html, page.inner_text("body")

        for n, url in enumerate(MARKETS, 1):
            print(f"\n######## Рынок: {url}")
            html, text = open_(url, f"m{n}_market")
            gens = list(dict.fromkeys(urljoin(url, h) for h in re.findall(r'href="([^"]*/g_\d+_\d+/)"', html)))
            print("Поколения:", gens[:15])
            print("--- текст страницы рынка (начало) ---\n", text[:2500])
            if not gens:
                continue
            g_html, g_text = open_(gens[0], f"m{n}_generation")
            print(f"\n=== Поколение {gens[0]} ===\n--- текст ---\n", g_text[:7000])
            print("--- HTML вокруг первой группы («л.с.») ---\n", around(g_html, "л.с.", 2500))
            mods = list(dict.fromkeys(urljoin(gens[0], h) for h in re.findall(r'href="([^"]+)"', g_html)
                                      if re.search(r"/catalog/[^/]+/[^/]+/\d+/?$", urljoin(gens[0], h))))
            print("Ссылки на комплектации:", mods[:10])
            if mods:
                m_html, m_text = open_(mods[0], f"m{n}_modification")
                print(f"\n=== Комплектация {mods[0]} ===\n--- текст ---\n", m_text[:5000])
                print("--- HTML вокруг «Мощность» ---\n", around(m_html, "Мощность", 1500))
        browser.close()


if __name__ == "__main__":
    main()
