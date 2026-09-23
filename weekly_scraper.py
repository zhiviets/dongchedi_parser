"""
Обход объявлений Dongchedi (подержанные машины Китая) и пуш в bn-auto.

Что делает:
  1. Открывает страницу списка подержанных машин и собирает объявления —
     и по ссылкам /usedcar/<id> на странице, и из JSON-ответов сайта,
     которые браузер получает сам (там уже есть год, цена и фото).
  2. Пропускает машины старше DONGCHEDI_MIN_YEAR и те, что уже есть в
     bn-auto с фото и характеристиками (для них обновляется только цена
     и отметка «ещё в продаже»).
  3. Новые машины разбирает через CarParser (main_page_parser.py):
     название, цена, пробег, фото, переведённая комплектация.
  4. Отправляет всё в bn-auto пачками.

Из песочницы, где писался этот скрипт, dongchedi.com недоступен, поэтому
на каждом прогоне в папку debug/ сохраняются скриншоты, HTML и сырые
JSON-ответы — они уходят в артефакт GitHub Actions «dongchedi-debug»,
и по ним дорабатывается разбор.

Запускать из корня репозитория (нужны config.json и
test_names_translation.json — их читает configuration_parser.py).
"""

import base64
import io
import json
import os
import random
import re
import time

import requests
from PIL import Image
from playwright.sync_api import sync_playwright

from main_page_parser import CarParser
from china_brand_map import extract_brand_model

# GitHub Actions подставляет несуществующий секрет пустой строкой — "or"
# ловит и отсутствие переменной, и пустое значение.
SEARCH_URLS = [u for u in [
    os.environ.get("DONGCHEDI_SEARCH_URL") or "",
    "https://www.dongchedi.com/usedcar/x-x-x-x-x-x-x-x-x-x-x-x-x/",
    "https://www.dongchedi.com/usedcar",
] if u]
TOTAL = int(os.environ.get("DONGCHEDI_TOTAL") or "30")
MIN_YEAR = int(os.environ.get("DONGCHEDI_MIN_YEAR") or "2020")
MAX_PAGES = int(os.environ.get("DONGCHEDI_MAX_PAGES") or "60")
# Одна машина = страница объявления + страница комплектации. Мёртвое или
# снятое объявление не должно держать прогон минутами.
LISTING_TIMEOUT_MS = int(os.environ.get("DONGCHEDI_LISTING_TIMEOUT_MS") or "30000")

BN_AUTO_URL = os.environ.get("BN_AUTO_URL", "").rstrip("/")
BN_AUTO_IMPORT_TOKEN = os.environ.get("BN_AUTO_IMPORT_TOKEN", "")
PROXY_SERVER = os.environ.get("PROXY_SERVER") or ""
PROXY_USERNAME = os.environ.get("PROXY_USERNAME") or ""
PROXY_PASSWORD = os.environ.get("PROXY_PASSWORD") or ""

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")

# Чуть меньше лимита bn-auto (900 КБ, см. server/photo.js) — запас на base64
MAX_PHOTO_BYTES = 850 * 1024

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(ROOT, "weekly_batch.json")
DEBUG_DIR = os.path.join(ROOT, "debug")


# ---------- мелочи ----------

def human_pause(a: float, b: float):
    time.sleep(random.uniform(a, b))


def save_debug(name: str, content):
    os.makedirs(DEBUG_DIR, exist_ok=True)
    path = os.path.join(DEBUG_DIR, name)
    mode = "wb" if isinstance(content, bytes) else "w"
    with open(path, mode, **({} if mode == "wb" else {"encoding": "utf-8"})) as f:
        if isinstance(content, (dict, list)):
            json.dump(content, f, ensure_ascii=False, indent=2)
        else:
            f.write(content)


def debug_page(page, name: str):
    """Скриншот и HTML страницы — по ним видно капчу, блокировку или новую вёрстку."""
    try:
        save_debug(f"{name}.png", page.screenshot(full_page=False))
        save_debug(f"{name}.html", page.content())
    except Exception as error:
        print(f"Не сохранён снимок {name}: {error}")


def year_from_title(title: str):
    m = re.search(r"(20\d{2})\s*款", title or "")
    return int(m.group(1)) if m else None


def to_units(value):
    """CarParser отдаёт цену/пробег в 万 (десятках тысяч) — переводим в целые единицы."""
    return round(value * 10_000) if value is not None else None


def _num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _year(v):
    m = re.search(r"(19|20)\d{2}", str(v or ""))
    return int(m.group(0)) if m else None


# ---------- список объявлений ----------

class SkuCollector:
    """Объявления из JSON-ответов сайта: id → то, что о нём известно из списка."""

    def __init__(self):
        self.items = {}
        self.samples = 0

    def on_response(self, response):
        try:
            if "dongchedi" not in response.url and "dcarapi" not in response.url:
                return
            if "json" not in (response.headers.get("content-type") or ""):
                return
            data = response.json()
        except Exception:
            return
        before = len(self.items)
        self._walk(data)
        if len(self.items) > before and self.samples < 3:
            self.samples += 1
            save_debug(f"list_api_{self.samples}.json", {"url": response.url, "body": data})

    def _walk(self, node):
        if isinstance(node, dict):
            sku = node.get("sku_id") or node.get("skuId")
            if sku and re.fullmatch(r"\d{5,}", str(sku)):
                self._add(str(sku), node)
            for v in node.values():
                self._walk(v)
        elif isinstance(node, list):
            for v in node:
                self._walk(v)

    def _add(self, sku: str, node: dict):
        title = node.get("title") or node.get("sku_title") or node.get("car_name") or ""
        info = self.items.setdefault(sku, {})
        info.setdefault("title", title)
        info.setdefault("year", _year(node.get("car_year") or node.get("year")) or year_from_title(title))
        price = _num(node.get("sh_price") or node.get("price"))
        if price:
            # В списках цена бывает и в 万, и в юанях
            info.setdefault("price", price * 10_000 if price < 10_000 else price)
        mileage = _num(node.get("mileage") or node.get("car_mileage"))
        if mileage is not None:
            info.setdefault("mileage", mileage * 10_000 if mileage < 100 else mileage)
        image = node.get("image") or node.get("cover_image") or node.get("image_url")
        if isinstance(image, str) and image.startswith("http"):
            info.setdefault("image", image)


def links_on_page(page) -> set[str]:
    hrefs = page.eval_on_selector_all("a[href*='/usedcar/']", "els => els.map(e => e.href)")
    return {m.group(1) for h in hrefs if (m := re.search(r"/usedcar/(\d{5,})", h))}


def go_next_page(page) -> bool:
    """Перейти на следующую страницу списка, если есть кнопка «下一页»."""
    for selector in ["a:has-text('下一页')", "button:has-text('下一页')", "li[class*='next'] a", "[class*='pagination'] [class*='next']"]:
        try:
            el = page.locator(selector).first
            if el.count() and el.is_visible() and "disabled" not in (el.get_attribute("class") or ""):
                el.click()
                page.wait_for_load_state("domcontentloaded", timeout=30_000)
                return True
        except Exception:
            continue
    return False


def discover(page, collector: SkuCollector, want: int) -> dict[str, dict]:
    """id объявления → данные из списка (год/цена/фото, если сайт их отдал)."""
    found: dict[str, dict] = {}
    for url in SEARCH_URLS:
        print(f"Список: {url}")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as error:
            print(f"  не открылся: {error}")
            continue
        human_pause(3, 5)
        debug_page(page, "list_page_1")
        for page_no in range(1, MAX_PAGES + 1):
            for _ in range(8):
                page.mouse.wheel(0, random.randint(900, 1600))
                page.wait_for_timeout(random.randint(400, 900))
            ids = links_on_page(page) | set(collector.items)
            fresh = ids - set(found)
            for sku in fresh:
                found[sku] = collector.items.get(sku, {})
            good = sum(1 for i in found.values() if (i.get("year") or MIN_YEAR) >= MIN_YEAR)
            print(f"  страница {page_no}: новых {len(fresh)}, всего {len(found)}, подходят по году {good}")
            if good >= want * 1.3 or not fresh and page_no > 1:
                break
            if not go_next_page(page):
                if page_no == 1:
                    debug_page(page, "list_no_next")
                break
            human_pause(4, 9)
        if found:
            break
        print("  объявлений не нашлось — пробуем следующий адрес")
    return found


# ---------- детали объявления ----------

def next_data(page) -> dict | None:
    try:
        raw = page.eval_on_selector("script#__NEXT_DATA__", "el => el.textContent")
        return json.loads(raw) if raw else None
    except Exception:
        return None


def parse_detail(page, parser: CarParser, sku: str, idx: int) -> dict:
    url = f"https://www.dongchedi.com/usedcar/{sku}"
    try:
        return parser.parse_car_page_using(page, url, save_local=False, timeout_ms=LISTING_TIMEOUT_MS)
    finally:
        if idx < 3:
            # Первые страницы — целиком в диагностику: HTML, скриншот и
            # встроенный JSON Next.js (если сайт его отдаёт)
            debug_page(page, f"detail_{sku}")
            data = next_data(page)
            if data:
                save_debug(f"next_data_{sku}.json", data)


# ---------- фото ----------

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


def fetch_primary_photo(session, image_urls: list[str]) -> str | None:
    """Скачать первое доступное фото и сохранить у нас: ссылки dongchedi подписаны и протухают."""
    for url in [u for u in image_urls if u][:3]:
        try:
            resp = session.get(url, headers={"Referer": "https://www.dongchedi.com/"}, timeout=20)
            resp.raise_for_status()
            data_url = compress_photo_to_data_url(resp.content)
            if data_url:
                return data_url
        except requests.RequestException:
            continue
    return None


# ---------- bn-auto ----------

def http_session():
    from urllib.parse import quote, urlsplit
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "image/webp,image/apng,image/*,*/*;q=0.8"})
    if PROXY_SERVER:
        parts = urlsplit(PROXY_SERVER)
        auth = f"{quote(PROXY_USERNAME, safe='')}:{quote(PROXY_PASSWORD, safe='')}@" if PROXY_USERNAME else ""
        proxy = f"{parts.scheme}://{auth}{parts.netloc}"
        s.proxies = {"http": proxy, "https": proxy}
    return s


def fetch_known() -> set[str]:
    if not BN_AUTO_URL or not BN_AUTO_IMPORT_TOKEN:
        return set()
    try:
        resp = requests.get(f"{BN_AUTO_URL}/api/live-listings/known", params={"source": "dongchedi"},
                            headers={"Authorization": f"Bearer {BN_AUTO_IMPORT_TOKEN}"}, timeout=30)
        resp.raise_for_status()
        ids = {str(i) for i in resp.json().get("ids") or []}
        print(f"Уже есть в bn-auto с фото и характеристиками: {len(ids)} — их страницы не открываем")
        return ids
    except Exception as error:
        print(f"Список известных объявлений не получен ({error}) — разбираем всё")
        return set()


def push_to_bn_auto(listings: list[dict]):
    if not BN_AUTO_URL or not BN_AUTO_IMPORT_TOKEN:
        print("BN_AUTO_URL / BN_AUTO_IMPORT_TOKEN не заданы — пуш в bn-auto пропущен.")
        return
    if not listings:
        print("Нет объявлений — нечего пушить.")
        return

    # С фото внутри пачка весит мегабайты — такие по 10; известные машины
    # (только цена и отметка «в продаже») — по 200.
    light = [x for x in listings if "photo_url" not in x]
    full = [x for x in listings if "photo_url" in x]
    batches = [light[i:i + 200] for i in range(0, len(light), 200)]
    batches += [full[i:i + 10] for i in range(0, len(full), 10)]
    done = 0
    for batch in batches:
        resp = requests.post(
            f"{BN_AUTO_URL}/api/live-listings/import",
            json={"source": "dongchedi", "listings": batch},
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
        print(f"Пуш в bn-auto [{done + 1}–{done + len(batch)}]: {data.get('stats')}, пропущено {data.get('skipped', 0)}")
        done += len(batch)


# ---------- прогон ----------

def scrape() -> list[dict]:
    known = fetch_known()
    session = http_session()
    listings: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        proxy = None
        if PROXY_SERVER:
            proxy = {"server": PROXY_SERVER, "username": PROXY_USERNAME or None, "password": PROXY_PASSWORD or None}
            proxy = {k: v for k, v in proxy.items() if v}
            print(f"Используем прокси: {PROXY_SERVER}")
        context = browser.new_context(
            user_agent=UA, viewport={"width": 1440, "height": 900},
            locale="zh-CN", timezone_id="Asia/Shanghai", proxy=proxy,
            extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6"},
        )
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")

        list_page = context.new_page()
        collector = SkuCollector()
        list_page.on("response", collector.on_response)
        found = discover(list_page, collector, TOTAL)
        list_page.close()
        print(f"Найдено объявлений: {len(found)} (из JSON сайта с данными: {len(collector.items)})")

        parser = CarParser()
        new_done = seen_known = skipped_year = failed = 0
        for sku, info in found.items():
            if new_done + seen_known >= TOTAL:
                break
            if failed >= 8 and new_done == 0:
                print("Ни одно из 8 объявлений не разобралось — останавливаемся, смотрите артефакт dongchedi-debug")
                break
            if info.get("year") and info["year"] < MIN_YEAR:
                skipped_year += 1
                continue
            url = f"https://www.dongchedi.com/usedcar/{sku}"
            if sku in known:
                # Уже на сайте — только отметка «ещё в продаже» и свежая цена
                item = {"external_id": sku, "source_url": url}
                if info.get("price"):
                    item["price_value"] = round(info["price"])
                listings.append(item)
                seen_known += 1
                continue

            page = context.new_page()
            try:
                car = parse_detail(page, parser, sku, new_done + failed)
            except Exception as error:
                failed += 1
                print(f"[{sku}] пропущено: {str(error).splitlines()[0][:200]}")
                continue
            finally:
                if not page.is_closed():
                    page.close()

            title = car.get("title") or info.get("title")
            price = to_units(car.get("price")) or (round(info["price"]) if info.get("price") else None)
            if not title or not price:
                failed += 1
                print(f"[{sku}] нет названия или цены — капча или новая вёрстка (см. артефакт dongchedi-debug)")
                continue
            year = year_from_title(title) or info.get("year")
            if year and year < MIN_YEAR:
                skipped_year += 1
                continue
            brand_en, model_guess, _ = extract_brand_model(title)
            spec = {c["name"]: c["value"] for c in (car.get("configuration_info") or []) if c.get("name")}
            listings.append({
                "external_id": sku,
                "make": brand_en,
                "model": model_guess,
                "title": title,
                "year": year,
                "mileage_km": to_units(car.get("mileage")) or (round(info["mileage"]) if info.get("mileage") else None),
                "price_value": price,
                "photo_url": fetch_primary_photo(session, (car.get("images") or []) + [info.get("image")]),
                "spec": spec or None,
                "source_url": url,
            })
            new_done += 1
            print(f"[{new_done}] {title} — {year}, характеристик: {len(spec)}")
            human_pause(3, 7)
            if new_done % 25 == 0:
                human_pause(25, 45)

        browser.close()

    print(f"Итого: новых {new_done}, уже на сайте {seen_known}, старше {MIN_YEAR} г. {skipped_year}, ошибок {failed}")
    return listings


def main():
    listings = scrape()
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump([{k: v for k, v in x.items() if k != "photo_url"} for x in listings], f, ensure_ascii=False, indent=2)
    print(f"Сохранено {len(listings)} объявлений -> {OUT_PATH}")
    push_to_bn_auto(listings)


if __name__ == "__main__":
    main()
