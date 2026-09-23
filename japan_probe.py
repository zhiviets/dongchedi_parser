"""
Проверка японских площадок подержанных машин для раздела «Япония»:
goo-net-exchange.com (англоязычная, для экспорта) и carsensor.net.

Пускают ли без капчи и входа, что есть на страницах списка и объявления.
Ничего не отправляет в bn-auto — только сохраняет в japan_debug/ скриншоты,
HTML и сводку (артефакт GitHub Actions «japan-probe»).
"""

import json
import os
import re
import time

from playwright.sync_api import sync_playwright

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "japan_debug")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")
SITES = {
    "goonet": {
        "lists": [
            "https://www.goo-net-exchange.com/usedcars/",
            "https://www.goo-net-exchange.com/usedcars/TOYOTA/",
        ],
        "detail_re": r"https?://www\.goo-net-exchange\.com/usedcars/[A-Z_\-]+/[A-Z0-9_\-]+/\d{6,}/?|/usedcars/[A-Z_\-]+/[A-Z0-9_\-]+/\d{6,}/?",
        "base": "https://www.goo-net-exchange.com",
        "locale": "en-US",
        "lang": "en-US,en;q=0.9",
    },
    "carsensor": {
        "lists": [
            "https://www.carsensor.net/usedcar/index.html",
            "https://www.carsensor.net/usedcar/bTO/index.html",
        ],
        "detail_re": r"/usedcar/detail/[A-Z]{2}\d+/index\.html",
        "base": "https://www.carsensor.net",
        "locale": "ja-JP",
        "lang": "ja-JP,ja;q=0.9",
    },
}
BLOCK_WORDS = ["captcha", "CAPTCHA", "Access Denied", "403 Forbidden", "アクセスが集中", "ロボット", "認証"]


def save(name, content):
    os.makedirs(OUT, exist_ok=True)
    mode = "wb" if isinstance(content, bytes) else "w"
    with open(os.path.join(OUT, name), mode, **({} if mode == "wb" else {"encoding": "utf-8"})) as f:
        f.write(content if not isinstance(content, (dict, list)) else json.dumps(content, ensure_ascii=False, indent=2))


def snapshot(page, name):
    try:
        save(f"{name}.png", page.screenshot(full_page=False))
        save(f"{name}.html", page.content())
    except Exception as error:
        print(f"  снимок {name} не сохранён: {error}")


def ld_json(html):
    """Структурированные данные schema.org — если есть, разбирать проще всего."""
    out = []
    for raw in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S)[:5]:
        try:
            out.append(json.loads(raw))
        except ValueError:
            out.append(raw[:500])
    return out


def probe_site(p, key, cfg, proxy, label):
    print(f"\n===== {key} / {label} =====")
    summary = {"site": key, "mode": label, "lists": [], "detail": None}
    browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
    context = browser.new_context(user_agent=UA, viewport={"width": 1440, "height": 900}, locale=cfg["locale"],
                                  timezone_id="Asia/Tokyo", proxy=proxy,
                                  extra_http_headers={"Accept-Language": cfg["lang"]})
    context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
    page = context.new_page()
    detail_url = None
    for i, url in enumerate(cfg["lists"], 1):
        info = {"url": url}
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(4000)
            for _ in range(3):
                page.mouse.wheel(0, 1400)
                page.wait_for_timeout(600)
            html = page.content()
            links = sorted(set(re.findall(cfg["detail_re"], html)))
            info.update({
                "status": resp.status if resp else None, "final_url": page.url, "title": page.title(),
                "blocked_words": [w for w in BLOCK_WORDS if w in html], "html_bytes": len(html),
                "detail_links": len(links), "detail_link_samples": links[:5],
                "ld_json": [str(x)[:800] for x in ld_json(html)],
                "text_sample": page.inner_text("body")[:1500],
            })
            snapshot(page, f"{key}_{label}_list_{i}")
            if links and not detail_url:
                detail_url = links[0] if links[0].startswith("http") else cfg["base"] + links[0]
        except Exception as error:
            info["error"] = str(error).splitlines()[0][:300]
            snapshot(page, f"{key}_{label}_list_{i}_error")
        print(json.dumps({k: v for k, v in info.items() if k != "text_sample"}, ensure_ascii=False, indent=2))
        summary["lists"].append(info)
        time.sleep(3)

    if detail_url:
        info = {"url": detail_url}
        try:
            resp = page.goto(detail_url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(5000)
            html = page.content()
            info.update({
                "status": resp.status if resp else None, "final_url": page.url, "title": page.title(),
                "blocked_words": [w for w in BLOCK_WORDS if w in html],
                "ld_json": [str(x)[:1500] for x in ld_json(html)],
                "images": sorted(set(re.findall(r"https?://[^\"'\s]+\.(?:jpg|jpeg|webp)", html)))[:8],
                "text_sample": page.inner_text("body")[:4000],
            })
            snapshot(page, f"{key}_{label}_detail")
        except Exception as error:
            info["error"] = str(error).splitlines()[0][:300]
            snapshot(page, f"{key}_{label}_detail_error")
        print(json.dumps({k: v for k, v in info.items() if k != "text_sample"}, ensure_ascii=False, indent=2))
        summary["detail"] = info
    else:
        print("Ссылок на объявления не найдено — страницу объявления не открываем")
    browser.close()
    save(f"{key}_{label}_summary.json", summary)


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
        for key, cfg in SITES.items():
            probe_site(p, key, cfg, None, "direct")
            if proxy:
                probe_site(p, key, cfg, proxy, "proxy")


if __name__ == "__main__":
    main()
