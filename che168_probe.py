"""
Проверка che168.com (Autohome, подержанные машины Китая): пускает ли сайт
без входа и что есть на страницах списка и объявления.

Ничего не отправляет в bn-auto — только сохраняет в папку che168_debug/
скриншоты, HTML и сводку (уходят в артефакт GitHub Actions «che168-probe»),
по ним пишется настоящий парсер. Прокси берётся из PROXY_* (если задан);
CHE168_DIRECT=1 — проверить и без прокси.
"""

import json
import os
import re
import time

from playwright.sync_api import sync_playwright

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "che168_debug")
LIST_URLS = [
    "https://www.che168.com/china/list/",
    "https://www.che168.com/china/a0_0msdgscncgpi1ltocsp1exx0/",
    "https://m.che168.com/china/list/",
]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")
BLOCK_WORDS = ["验证", "安全检测", "访问异常", "请登录", "滑块", "captcha", "403 Forbidden"]


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


def blocked_words(html):
    return [w for w in BLOCK_WORDS if w in html]


def probe(label, proxy):
    print(f"\n===== {label} =====")
    summary = {"mode": label, "lists": [], "detail": None, "json_responses": []}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(
            user_agent=UA, viewport={"width": 1440, "height": 900}, locale="zh-CN",
            timezone_id="Asia/Shanghai", proxy=proxy,
            extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6"},
        )
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        page = context.new_page()

        def on_response(resp):
            try:
                if "json" in (resp.headers.get("content-type") or "") and len(summary["json_responses"]) < 15:
                    body = resp.text()
                    summary["json_responses"].append({"url": resp.url, "status": resp.status, "sample": body[:3000]})
            except Exception:
                pass
        page.on("response", on_response)

        detail_url = None
        for i, url in enumerate(LIST_URLS, 1):
            info = {"url": url}
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(4000)
                for _ in range(4):
                    page.mouse.wheel(0, 1400)
                    page.wait_for_timeout(700)
                html = page.content()
                info.update({
                    "status": resp.status if resp else None,
                    "final_url": page.url,
                    "title": page.title(),
                    "blocked_words": blocked_words(html),
                    "html_bytes": len(html),
                })
                links = sorted(set(re.findall(r"/dealer/\d+/\d+\.html", html)))
                info["detail_links"] = len(links)
                info["detail_link_samples"] = links[:5]
                # Атрибуты карточек списка — по ним видно, что сайт отдаёт прямо в списке
                li = page.query_selector("li[infoid], li.cards-li, [data-infoid]")
                if li:
                    info["card_attributes"] = page.evaluate(
                        "el => Object.fromEntries([...el.attributes].map(a => [a.name, a.value.slice(0, 200)]))", li)
                    info["card_text"] = (li.inner_text() or "")[:500]
                snapshot(page, f"{label}_list_{i}")
                if links and not detail_url:
                    detail_url = "https://www.che168.com" + links[0]
            except Exception as error:
                info["error"] = str(error).splitlines()[0][:300]
                snapshot(page, f"{label}_list_{i}_error")
            print(json.dumps(info, ensure_ascii=False, indent=2))
            summary["lists"].append(info)
            time.sleep(3)

        if detail_url:
            info = {"url": detail_url}
            try:
                resp = page.goto(detail_url, wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(5000)
                html = page.content()
                text = page.inner_text("body")[:4000]
                info.update({
                    "status": resp.status if resp else None, "final_url": page.url, "title": page.title(),
                    "blocked_words": blocked_words(html),
                    # что есть на странице: цена, пробег, дата регистрации, объём, коробка, ссылка на параметры
                    "has": {k: bool(re.search(v, text)) for k, v in {
                        "price_万": r"\d+(\.\d+)?\s*万", "mileage_万公里": r"万公里", "reg_date_上牌": r"上牌",
                        "displacement_排量": r"排量", "gearbox_变速箱": r"变速箱", "config_参数": r"参数配置|查看配置",
                    }.items()},
                    "config_links": sorted(set(re.findall(r"(?:https?:)?//[^\"'\s]*(?:config|spec)[^\"'\s]*", html)))[:8],
                    "images": sorted(set(re.findall(r"//[^\"'\s]*autoimg\.cn[^\"'\s]*\.(?:jpg|jpeg|webp|png)", html)))[:5],
                    "text_sample": text[:1500],
                })
                snapshot(page, f"{label}_detail")
            except Exception as error:
                info["error"] = str(error).splitlines()[0][:300]
                snapshot(page, f"{label}_detail_error")
            print(json.dumps({k: v for k, v in info.items() if k != "text_sample"}, ensure_ascii=False, indent=2))
            summary["detail"] = info
        else:
            print("Ссылок на объявления не найдено — страницу объявления не открываем")

        browser.close()
    save(f"{label}_summary.json", summary)


def main():
    server = os.environ.get("PROXY_SERVER") or ""
    if server:
        proxy = {"server": server}
        if os.environ.get("PROXY_USERNAME"):
            proxy["username"] = os.environ["PROXY_USERNAME"]
        if os.environ.get("PROXY_PASSWORD"):
            proxy["password"] = os.environ["PROXY_PASSWORD"]
        probe("proxy", proxy)
    if not server or os.environ.get("CHE168_DIRECT") == "1":
        probe("direct", None)


if __name__ == "__main__":
    main()
