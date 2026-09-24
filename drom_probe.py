"""
Проверка каталога drom.ru: пускает ли сервер (напрямую и через российский
прокси), как устроены страницы марки → модели → поколения → комплектации и
есть ли на них мощность. Ничего не отправляет на сайт — только сохраняет в
drom_debug/ HTML и сводку (артефакт GitHub Actions «drom-probe»).
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
START = [
    "https://www.drom.ru/catalog/",
    "https://www.drom.ru/catalog/hyundai/avante/",
    "https://www.drom.ru/catalog/kia/k5/",
    "https://www.drom.ru/catalog/toyota/camry/",
    "https://www.drom.ru/catalog/geely/monjaro/",
    "https://www.drom.ru/catalog/haval/h6/",
]
POWER_RE = re.compile(r"(\d{2,4})\s*л\.\s*с\.")


def save(name, content):
    os.makedirs(OUT, exist_ok=True)
    mode = "w" if isinstance(content, str) else "wb"
    with open(os.path.join(OUT, name), mode, **({"encoding": "utf-8"} if mode == "w" else {})) as f:
        f.write(content)


def links_under(html, base, prefix):
    out = []
    for href in re.findall(r'href="([^"#?]+)"', html):
        url = urljoin(base, href)
        if url.startswith(prefix) and url.rstrip("/") != prefix.rstrip("/"):
            out.append(url)
    return list(dict.fromkeys(out))


def probe(p, proxy, label):
    print(f"\n===== drom.ru / {label} =====")
    browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
    context = browser.new_context(user_agent=UA, locale="ru-RU", timezone_id="Asia/Vladivostok", proxy=proxy,
                                  extra_http_headers={"Accept-Language": "ru-RU,ru;q=0.9"})
    page = context.new_page()
    summary = []
    n = 0

    def visit(url, depth):
        nonlocal n
        n += 1
        info = {"url": url, "depth": depth}
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(2500)
            html = page.content()
            text = page.inner_text("body")
            info.update({"status": resp.status if resp else None, "final": page.url, "title": page.title(),
                         "bytes": len(html), "power_values": POWER_RE.findall(text)[:15],
                         "text": text[:1500]})
            save(f"{label}_{n:02d}.html", html)
            info["links"] = links_under(html, url, url if depth else "https://www.drom.ru/catalog/")[:40]
        except Exception as error:
            info["error"] = str(error).splitlines()[0][:300]
        print(json.dumps({k: v for k, v in info.items() if k not in ("text", "links")}, ensure_ascii=False))
        print("  ссылки:", (info.get("links") or [])[:12])
        summary.append(info)
        time.sleep(3)
        return info

    for url in START:
        top = visit(url, 0)
        if url != START[0]:
            # Вглубь: поколение → комплектация (первые ссылки под адресом модели)
            for sub in (top.get("links") or [])[:2]:
                mid = visit(sub, 1)
                for leaf in (mid.get("links") or [])[:2]:
                    visit(leaf, 2)
    save(f"summary_{label}.json", json.dumps(summary, ensure_ascii=False, indent=2))
    browser.close()


def main():
    server = os.environ.get("PROXY_SERVER") or ""
    proxy = None
    if server:
        proxy = {"server": server}
        if os.environ.get("PROXY_USERNAME"):
            proxy["username"] = os.environ["PROXY_USERNAME"]
        if os.environ.get("PROXY_PASSWORD"):
            proxy["password"] = os.environ["PROXY_PASSWORD"]
    with sync_playwright() as p:
        probe(p, None, "direct")
        if proxy:
            probe(p, proxy, "proxy")


if __name__ == "__main__":
    main()
