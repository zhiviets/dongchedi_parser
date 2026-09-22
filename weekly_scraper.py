"""
Еженедельный обход каталога Dongchedi: находит объявления на странице
списка/поиска и парсит каждое через уже рабочий CarParser
(main_page_parser.py), затем пушит результат в bn-auto.

ВАЖНО про DONGCHEDI_SEARCH_URL: значение по умолчанию ниже — рабочая
догадка по схеме URL, которую dongchedi.com использует для списков без
фильтров (по аналогии с остальными сайтами того же поколения). Из
песочницы, где писался этот скрипт, нет доступа в интернет к
dongchedi.com, так что подтвердить ссылку вживую было нельзя — откройте
её в браузере и, если она ведёт не туда (или её структура изменилась),
замените через переменную окружения/секрет DONGCHEDI_SEARCH_URL.

Запускать из корня репозитория (нужны config.json и
test_names_translation.json рядом — их использует configuration_parser.py).
"""

import base64
import io
import json
import os
import re
import time

import requests
from PIL import Image
from playwright.sync_api import sync_playwright

from main_page_parser import CarParser
from china_brand_map import extract_brand_model

SEARCH_URL = os.environ.get(
    "DONGCHEDI_SEARCH_URL",
    "https://www.dongchedi.com/usedcar/x-x-x-x-x-x-x-x-x-x-x-x-x/",
)
MAX_LISTINGS = int(os.environ.get("DONGCHEDI_MAX_LISTINGS", "30"))
# Одна машина = полная загрузка страницы + переход на страницу комплектации,
# на медленной сети это не быстро — не даём одному зависшему объявлению
# держать весь еженедельный прогон часами.
LISTING_TIMEOUT_MS = int(os.environ.get("DONGCHEDI_LISTING_TIMEOUT_MS", "25000"))

BN_AUTO_URL = os.environ.get("BN_AUTO_URL", "").rstrip("/")
BN_AUTO_IMPORT_TOKEN = os.environ.get("BN_AUTO_IMPORT_TOKEN", "")

# Чуть меньше лимита bn-auto (900 КБ, см. server/photo.js) — запас на
# накладные расходы base64.
MAX_PHOTO_BYTES = 850 * 1024

OUT_PATH = os.path.join(os.path.dirname(__file__), "weekly_batch.json")


def discover_listing_urls(page, search_url: str, max_listings: int) -> list[str]:
    """Собрать ссылки на карточки со страницы списка.

    Специально не завязываемся на конкретные CSS-классы карточки — на
    сайтах этого поколения (React + CSS-модули) они меняются при каждом
    редеплое фронтенда, именно из-за этого не работает большинство
    скриптов в che168-parser. Вместо этого берём любые ссылки вида
    /usedcar/<цифры> — этот паттерн уже подтверждён рабочим парсером
    одной карточки (main_page_parser.py парсит именно такие URL).
    """
    page.goto(search_url, wait_until="networkidle", timeout=60_000)
    time.sleep(1.5)

    seen = set()
    stable_rounds = 0
    for _ in range(20):
        hrefs = page.eval_on_selector_all("a[href*='/usedcar/']", "els => els.map(e => e.href)")
        before = len(seen)
        for href in hrefs:
            m = re.search(r"/usedcar/(\d+)", href)
            if m:
                seen.add(f"https://www.dongchedi.com/usedcar/{m.group(1)}")
        if len(seen) >= max_listings:
            break
        if len(seen) == before:
            stable_rounds += 1
            if stable_rounds >= 3:
                break
        else:
            stable_rounds = 0
        page.mouse.wheel(0, 2400)
        page.wait_for_timeout(900)

    return list(seen)[:max_listings]


def year_from_title(title: str):
    m = re.search(r"(\d{4})\s*款", title or "")
    return int(m.group(1)) if m else None


def to_units(value):
    """CarParser отдаёт цену/пробег в 万 (десятках тысяч) — переводим в целые единицы."""
    return round(value * 10_000) if value is not None else None


def compress_photo_to_data_url(image_bytes: bytes) -> str | None:
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception:
        return None

    quality = 82
    max_width = 1000
    while True:
        resized = img
        if resized.width > max_width:
            ratio = max_width / resized.width
            resized = resized.resize((max_width, max(1, int(resized.height * ratio))))
        buf = io.BytesIO()
        resized.save(buf, format="JPEG", quality=quality)
        data = buf.getvalue()
        if len(data) <= MAX_PHOTO_BYTES or (quality <= 40 and max_width <= 480):
            return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
        if quality > 40:
            quality -= 12
        else:
            max_width = int(max_width * 0.8)


def fetch_primary_photo(image_urls: list[str]) -> str | None:
    """Скачать и переупаковать первое доступное фото в data-URL.

    Ссылки на фото у dongchedi подписаны и протухают (см. x-expires в
    query), поэтому просто сохранить исходный URL в базе нельзя — фото
    скачивается один раз при парсинге и хранится у нас (как и остальной
    каталог bn-auto: см. комментарий в server/photo.js).
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.dongchedi.com/",
        "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
    }
    for url in (image_urls or [])[:3]:
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            data_url = compress_photo_to_data_url(resp.content)
            if data_url:
                return data_url
        except requests.RequestException:
            continue
    return None


def scrape_batch() -> list[dict]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            )
        )

        list_page = context.new_page()
        urls = discover_listing_urls(list_page, SEARCH_URL, MAX_LISTINGS)
        list_page.close()
        print(f"Найдено объявлений: {len(urls)}")

        parser = CarParser()
        results = []
        for i, url in enumerate(urls, start=1):
            m = re.search(r"/usedcar/(\d+)", url)
            external_id = m.group(1) if m else None
            if not external_id:
                continue

            try:
                detail_page = context.new_page()
                try:
                    car = parser.parse_car_page_using(
                        detail_page, url, save_local=False, timeout_ms=LISTING_TIMEOUT_MS
                    )
                finally:
                    detail_page.close()
            except Exception as error:
                print(f"[{i}/{len(urls)}] Пропущено {url}: {error}")
                continue

            brand_en, model_guess, _ = extract_brand_model(car.get("title"))
            spec = {
                p["name"]: p["value"]
                for p in (car.get("configuration_info") or [])
                if p.get("name")
            }

            results.append({
                "external_id": external_id,
                "make": brand_en,
                "model": model_guess,
                "title": car.get("title"),
                "year": year_from_title(car.get("title")),
                "mileage_km": to_units(car.get("mileage")),
                "price_value": to_units(car.get("price")),
                "photo_url": fetch_primary_photo(car.get("images")),
                "spec": spec or None,
                "source_url": url,
            })
            print(f"[{i}/{len(urls)}] OK: {car.get('title')}")

        browser.close()
        return results


def push_to_bn_auto(listings: list[dict]):
    if not BN_AUTO_URL or not BN_AUTO_IMPORT_TOKEN:
        print("BN_AUTO_URL / BN_AUTO_IMPORT_TOKEN не заданы — пуш в bn-auto пропущен.")
        return
    if not listings:
        print("Нет объявлений — нечего пушить.")
        return

    resp = requests.post(
        f"{BN_AUTO_URL}/api/live-listings/import",
        json={"source": "dongchedi", "listings": listings},
        headers={"Authorization": f"Bearer {BN_AUTO_IMPORT_TOKEN}"},
        timeout=60,
    )
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code >= 400:
        print(f"Пуш в bn-auto не удался: HTTP {resp.status_code} {data}")
        resp.raise_for_status()
    print(f"Пуш в bn-auto: {data.get('stats')}, пропущено {data.get('skipped', 0)}")


def main():
    listings = scrape_batch()
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(listings, f, ensure_ascii=False, indent=2)
    print(f"Сохранено {len(listings)} объявлений -> {OUT_PATH}")
    push_to_bn_auto(listings)


if __name__ == "__main__":
    main()
