"""
Подержанные машины Китая с che168.com (Autohome) → bn-auto.

Как работает:
  1. Листает общий список che168 (/china/…sp<N>exx0/, 56 машин на странице).
     В разметке каждой карточки уже есть id, название, цена (万 юаней),
     пробег (万 км), дата регистрации — машины старше CHE168_MIN_YEAR
     отсеиваются сразу, без открытия объявления.
  2. Машины, которые уже есть в bn-auto с фото и характеристиками, не
     открываются: для них обновляются только цена, пробег и отметка
     «ещё в продаже».
  3. У новых открывается страница объявления: мощность и двигатель
     («2.0T 224马力 L4»), коробка, привод, объём, класс, цвет, владельцы,
     фото — переводится на русский.
  4. Всё уходит в bn-auto пачками (source = che168, раздел «Китай»).

Проверено на реальных страницах (артефакт che168-probe): вход и капча
не нужны ни с IP GitHub, ни через прокси.
"""

import html as html_lib
import json
import os
import random
import re
import time

import requests
from playwright.sync_api import sync_playwright

from china_brand_map import extract_brand_model
from weekly_scraper import compress_photo_to_data_url

TOTAL = int(os.environ.get("CHE168_TOTAL") or "30")
MIN_YEAR = int(os.environ.get("CHE168_MIN_YEAR") or "2020")
MAX_PAGES = int(os.environ.get("CHE168_MAX_PAGES") or "100")
LIST_URL = "https://www.che168.com/china/a0_0msdgscncgpi1ltocsp{page}exx0/"

BN_AUTO_URL = os.environ.get("BN_AUTO_URL", "").rstrip("/")
BN_AUTO_IMPORT_TOKEN = os.environ.get("BN_AUTO_IMPORT_TOKEN", "")
# Прокси необязателен: che168 пускает и IP GitHub. CHE168_USE_PROXY=1 — через PROXY_*.
USE_PROXY = os.environ.get("CHE168_USE_PROXY") == "1"
PROXY_SERVER = os.environ.get("PROXY_SERVER") or ""
PROXY_USERNAME = os.environ.get("PROXY_USERNAME") or ""
PROXY_PASSWORD = os.environ.get("PROXY_PASSWORD") or ""

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")
ROOT = os.path.dirname(os.path.abspath(__file__))
DEBUG_DIR = os.path.join(ROOT, "che168_debug")
OUT_PATH = os.path.join(ROOT, "che168_batch.json")

# ---------- перевод ----------

GEARBOX = {"自动": "автомат", "手动": "механика", "手自一体": "автомат", "双离合": "робот", "无级变速": "вариатор", "CVT": "вариатор"}
DRIVE = [(r"四驱|全时|适时|分时", "полный"), (r"前驱", "передний"), (r"后驱", "задний")]
BODY = {
    "微型车": "микро (A)", "小型车": "малый класс (B)", "紧凑型车": "компакт (C)", "中型车": "средний класс (D)",
    "中大型车": "бизнес-класс (E)", "大型车": "представительский (F)", "跑车": "спорткар",
    "小型SUV": "малый SUV", "紧凑型SUV": "компактный SUV", "中型SUV": "средний SUV", "中大型SUV": "большой SUV",
    "大型SUV": "полноразмерный SUV", "MPV": "минивэн", "紧凑型MPV": "минивэн", "中型MPV": "минивэн",
    "中大型MPV": "минивэн", "大型MPV": "минивэн", "皮卡": "пикап", "微面": "микроавтобус", "轻客": "микроавтобус",
}
COLOR = {
    "黑色": "чёрный", "白色": "белый", "灰色": "серый", "深灰色": "тёмно-серый", "银色": "серебристый", "银灰色": "серебристо-серый",
    "蓝色": "синий", "深蓝色": "тёмно-синий", "红色": "красный", "棕色": "коричневый", "咖啡色": "коричневый",
    "绿色": "зелёный", "黄色": "жёлтый", "橙色": "оранжевый", "紫色": "фиолетовый", "香槟色": "шампань",
    "金色": "золотистый", "粉色": "розовый", "其它": "другой", "其他": "другой",
}
EMISSION = {"国VI": "Китай 6", "国六": "Китай 6", "国V": "Китай 5", "国五": "Китай 5", "国IV": "Китай 4"}
MONTHS = ["янв", "февр", "март", "апр", "май", "июнь", "июль", "авг", "сент", "окт", "нояб", "дек"]
HAN = re.compile(r"[一-鿿]")


def fuel_of(name: str, engine: str) -> str:
    text = f"{name} {engine}"
    if re.search(r"纯电|电动|kWh", text) and not re.search(r"插电|增程|混动|DM-?i|DM-?p|PHEV", text, re.I):
        return "Электро"
    if re.search(r"增程", text):
        return "Последовательный гибрид (увеличенный запас хода)"
    if re.search(r"插电|混动|DM-?i|DM-?p|PHEV|HEV|油电|双擎|e:HEV|锐·混动", text, re.I):
        return "Гибрид"
    if re.search(r"柴油", text):
        return "Дизель"
    return "Бензин"


def latin_trim(name: str):
    """Латинская часть комплектации после «2022款»: «45 TFSI», «xDrive 40Li M»."""
    tail = re.split(r"\d{4}\s*款", name, maxsplit=1)
    words = [w for w in (tail[1] if len(tail) > 1 else "").split() if not HAN.search(w)]
    return " ".join(words) or None


def release(reg: str):
    m = re.search(r"(\d{4})\D+(\d{1,2})", reg or "")
    if not m:
        return None
    month = int(m.group(2))
    return f"{MONTHS[month - 1]} {m.group(1)}" if 1 <= month <= 12 else m.group(1)


def _num(v):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


# ---------- мелочи ----------

def human_pause(a, b):
    time.sleep(random.uniform(a, b))


def save_debug(name, content):
    os.makedirs(DEBUG_DIR, exist_ok=True)
    mode = "wb" if isinstance(content, bytes) else "w"
    with open(os.path.join(DEBUG_DIR, name), mode, **({} if mode == "wb" else {"encoding": "utf-8"})) as f:
        f.write(content)


class Blocked(Exception):
    pass


# ---------- список ----------

CARD_RE = re.compile(r"<li[^>]*\binfoid=\"(\d+)\"[^>]*>", re.S)


def parse_cards(page_html: str) -> list[dict]:
    cards = []
    for m in CARD_RE.finditer(page_html):
        tag = m.group(0)
        attrs = {k: html_lib.unescape(v) for k, v in re.findall(r'([\w-]+)="([^"]*)"', tag)}
        if not attrs.get("carname") or not attrs.get("dealerid"):
            continue
        # ссылка на фото — внутри карточки, после открывающего тега
        chunk = page_html[m.end(): m.end() + 3000]
        img = re.search(r'<img[^>]+src="([^"]+autohomecar__[^"]+)"', chunk)
        price = _num(attrs.get("price"))
        mileage = _num(attrs.get("milage"))
        reg = attrs.get("regdate") or ""
        year = int(reg[:4]) if re.match(r"\d{4}", reg) else None
        if not year or year < 1990:
            # «1900/01» — ещё не зарегистрирована (новая машина): год модели из названия
            model_year = re.search(r"(20\d{2})\s*款", attrs.get("carname", ""))
            year = int(model_year.group(1)) if model_year else None
            reg = ""
        cards.append({
            "infoid": attrs["infoid"] if "infoid" in attrs else m.group(1),
            "dealerid": attrs["dealerid"],
            "name": attrs["carname"],
            "price_cny": round(price * 10_000) if price else None,
            "mileage_km": round(mileage * 10_000) if mileage is not None else None,
            "regdate": reg,
            "year": year,
            "image": ("https:" + img.group(1)) if img and img.group(1).startswith("//") else (img.group(1) if img else None),
        })
    return cards


def collect(page, known: set, want: int) -> list[dict]:
    """Карточки 2020+ со страниц списка: новых — сколько нужно, известных — все встреченные."""
    seen, picked = set(), []
    new_count = 0
    for page_no in range(1, MAX_PAGES + 1):
        url = LIST_URL.format(page=page_no)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(random.randint(2500, 4000))
            content = page.content()
        except Exception as error:
            print(f"Список, страница {page_no}: не открылась ({str(error).splitlines()[0][:150]})")
            break
        cards = parse_cards(content)
        if page_no == 1:
            save_debug("list_page_1.html", content)
        if not cards:
            print(f"Список, страница {page_no}: карточек нет — конец списка или блокировка")
            save_debug(f"list_page_{page_no}_empty.html", content)
            break
        fresh = [c for c in cards if c["infoid"] not in seen]
        good = [c for c in fresh if c["year"] and c["year"] >= MIN_YEAR]
        for c in fresh:
            seen.add(c["infoid"])
        for c in good:
            if c["infoid"] in known:
                picked.append(c)
            elif new_count < want:
                picked.append(c)
                new_count += 1
        print(f"Список, страница {page_no}: карточек {len(cards)}, с {MIN_YEAR} г. {len(good)}, новых набрано {new_count}/{want}")
        if new_count >= want:
            break
        human_pause(3, 7)
    return picked


# ---------- объявление ----------

ROW_RE = re.compile(r'<span class="item-name">(.*?)</span>(.*?)</li>', re.S)


def _clean(text):
    text = html_lib.unescape(re.sub(r"<[^>]+>", "", text or ""))
    return re.sub(r"\s+", " ", text.replace("\xa0", "")).strip()


def decode_html(raw: bytes, content_type: str = "") -> str:
    """che168 отдаёт страницы в GB2312 — читаем их в GB18030 (надмножество), остальное в UTF-8."""
    head = raw[:4000].decode("ascii", "ignore").lower()
    charset = re.search(r"charset=([\w-]+)", content_type.lower()) or re.search(r"charset=[\"']?([\w-]+)", head)
    name = charset.group(1) if charset else "utf-8"
    if name in ("gb2312", "gbk", "gb18030"):
        name = "gb18030"
    try:
        return raw.decode(name, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def fetch_detail(context, car: dict) -> str:
    url = f"https://www.che168.com/dealer/{car['dealerid']}/{car['infoid']}.html"
    resp = context.request.get(url, headers={"Referer": "https://www.che168.com/china/list/"}, timeout=45_000)
    body = decode_html(resp.body(), resp.headers.get("content-type", ""))
    if resp.status in (403, 429) or ("item-name" not in body and ("验证码" in body or "安全验证" in body)):
        raise Blocked(f"HTTP {resp.status}")
    return body


def parse_detail(body: str, car: dict) -> dict:
    rows = {}
    for k, v in ROW_RE.findall(body):
        rows[_clean(k).replace(" ", "")] = _clean(v)
    name = car["name"]
    engine = rows.get("发动机", "")
    hp = re.search(r"(\d{2,4})\s*马力", engine)
    liters = re.search(r"(\d+(?:\.\d+)?)\s*[LT]", engine) or re.search(r"(\d+(?:\.\d+)?)\s*L", rows.get("排量", ""))
    fuel = fuel_of(name, engine)
    cc = round(float(liters.group(1)) * 1000) if liters and fuel != "Электро" else None
    body_cls = rows.get("车辆级别", "")
    color = rows.get("车身颜色", "")
    owners = re.match(r"(\d+)", rows.get("过户次数", ""))
    engine_ru = re.sub(r"\s*马力", " л.с.", engine).strip()
    spec = {
        "Лот": car["infoid"],
        "Первая регистрация": release(rows.get("上牌时间") or car["regdate"]),
        "Пробег": f"{car['mileage_km']:,} км".replace(",", " ") if car.get("mileage_km") is not None else None,
        "Модификация": latin_trim(name),
        "Двигатель": None if HAN.search(engine_ru) else engine_ru or None,
        "Тип топлива": fuel,
        "Рабочий объём цилиндров (см³)": str(cc) if cc else None,
        "Коробка передач": GEARBOX.get((rows.get("变速箱") or rows.get("挡位/排量", "").split("/")[0]).strip()),
        "Привод": next((ru for rx, ru in DRIVE if re.search(rx, rows.get("驱动方式", ""))), None),
        "Тип кузова": BODY.get(body_cls),
        "Цвет": COLOR.get(color),
        "Экологический класс": EMISSION.get(rows.get("排放标准", "")),
        "Владельцев": str(int(owners.group(1)) + 1) if owners else None,
    }
    if hp and not spec["Двигатель"]:
        spec["Двигатель"] = f"{hp.group(1)} л.с."
    # Фото: сначала полноразмерные из галереи объявления, потом из карточки списка
    photos = re.findall(r"//[\w.]*autoimg\.cn/escimg/[^\"'\s]*?f_s_autohomecar__[^\"'\s]+?\.jpg", body)
    return {
        "spec": {k: v for k, v in spec.items() if v},
        "photos": ["https:" + p for p in dict.fromkeys(photos)][:3],
    }


def brand_model(name: str, body: str):
    """Марка и модель из названия; если марки в нём нет («福克斯(进口)») — из «хлебных крошек» объявления."""
    make, model, _ = extract_brand_model(name)
    if make:
        return make, model
    from china_brand_map import BRAND_MAP
    for crumb in re.findall(r"二手([^<>\s]{1,12})</a>", body):
        for key in sorted(BRAND_MAP, key=len, reverse=True):
            if crumb.startswith(key):
                make2, model2, _ = extract_brand_model(key + name)
                return make2, model2
    return None, None


# ---------- bn-auto ----------

def http_session():
    from urllib.parse import quote, urlsplit
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": "https://www.che168.com/"})
    if USE_PROXY and PROXY_SERVER:
        parts = urlsplit(PROXY_SERVER)
        auth = f"{quote(PROXY_USERNAME, safe='')}:{quote(PROXY_PASSWORD, safe='')}@" if PROXY_USERNAME else ""
        s.proxies = {"http": f"{parts.scheme}://{auth}{parts.netloc}", "https": f"{parts.scheme}://{auth}{parts.netloc}"}
    return s


def fetch_photo(session, urls):
    for url in [u for u in urls if u][:4]:
        try:
            resp = session.get(url, timeout=20)
            resp.raise_for_status()
            data_url = compress_photo_to_data_url(resp.content)
            if data_url:
                return data_url
        except requests.RequestException:
            continue
    return None


def fetch_known() -> set:
    if not BN_AUTO_URL or not BN_AUTO_IMPORT_TOKEN:
        return set()
    try:
        resp = requests.get(f"{BN_AUTO_URL}/api/live-listings/known", params={"source": "che168"},
                            headers={"Authorization": f"Bearer {BN_AUTO_IMPORT_TOKEN}"}, timeout=30)
        resp.raise_for_status()
        ids = {str(i) for i in resp.json().get("ids") or []}
        print(f"Уже есть в bn-auto с фото и характеристиками: {len(ids)} — их объявления не открываем")
        return ids
    except Exception as error:
        print(f"Список известных объявлений не получен ({error}) — разбираем всё")
        return set()


def push(listings):
    if not BN_AUTO_URL or not BN_AUTO_IMPORT_TOKEN:
        print("BN_AUTO_URL / BN_AUTO_IMPORT_TOKEN не заданы — пуш в bn-auto пропущен.")
        return
    light = [x for x in listings if "spec" not in x]
    full = [x for x in listings if "spec" in x]
    batches = [light[i:i + 200] for i in range(0, len(light), 200)] + [full[i:i + 10] for i in range(0, len(full), 10)]
    done = 0
    for batch in batches:
        resp = requests.post(f"{BN_AUTO_URL}/api/live-listings/import", json={"source": "che168", "listings": batch},
                             headers={"Authorization": f"Bearer {BN_AUTO_IMPORT_TOKEN}"}, timeout=60)
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

def main():
    known = fetch_known()
    session = http_session()
    listings = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        proxy = None
        if USE_PROXY and PROXY_SERVER:
            proxy = {k: v for k, v in {"server": PROXY_SERVER, "username": PROXY_USERNAME, "password": PROXY_PASSWORD}.items() if v}
            print(f"Используем прокси: {PROXY_SERVER}")
        context = browser.new_context(user_agent=UA, viewport={"width": 1440, "height": 900}, locale="zh-CN",
                                      timezone_id="Asia/Shanghai", proxy=proxy,
                                      extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6"})
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        page = context.new_page()
        cars = collect(page, known, TOTAL)
        print(f"Отобрано: {len(cars)} (уже на сайте: {sum(c['infoid'] in known for c in cars)})")

        done = failed = 0
        breaks = random.randint(20, 30)
        for car in cars:
            url = f"https://www.che168.com/dealer/{car['dealerid']}/{car['infoid']}.html"
            if car["infoid"] in known:
                listings.append({"external_id": car["infoid"], "price_value": car["price_cny"],
                                 "mileage_km": car["mileage_km"], "source_url": url})
                continue
            try:
                body = fetch_detail(context, car)
            except Blocked as reason:
                print(f"che168 притормозил нас ({reason}) — пауза 90–150 с и одна попытка")
                human_pause(90, 150)
                try:
                    body = fetch_detail(context, car)
                except Exception as again:
                    print(f"Снова не пускает ({again}) — останавливаемся, отправляем собранное")
                    break
            except Exception as error:
                failed += 1
                print(f"[{car['infoid']}] не открылось: {str(error).splitlines()[0][:150]}")
                if failed >= 8 and done == 0:
                    print("Не открылось ни одно из 8 объявлений — останавливаемся")
                    break
                continue
            if done < 2:
                save_debug(f"detail_{car['infoid']}.html", body)
            d = parse_detail(body, car)
            make, model = brand_model(car["name"], body)
            listings.append({
                "external_id": car["infoid"],
                "make": make,
                "model": model,
                "title": car["name"],
                "year": car["year"],
                "mileage_km": car["mileage_km"],
                "price_value": car["price_cny"],
                "photo_url": fetch_photo(session, d["photos"] + [car.get("image")]),
                "spec": d["spec"] or None,
                "source_url": url,
            })
            done += 1
            print(f"[{done}] {make or '?'} {model or ''} {car['year']} — {car['price_cny']} ¥, характеристик: {len(d['spec'])}")
            human_pause(2, 5)
            if done >= breaks:
                human_pause(25, 45)
                breaks = done + random.randint(20, 30)
        browser.close()

    print(f"Итого: новых {sum('spec' in x for x in listings)}, уже на сайте {sum('spec' not in x for x in listings)}, ошибок {failed}")
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump([{k: v for k, v in x.items() if k != "photo_url"} for x in listings], f, ensure_ascii=False, indent=2)
    push(listings)


if __name__ == "__main__":
    main()
