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
# Отдельный (китайский) прокси для che168 — CHE168_PROXY_*; если его нет — общий PROXY_*.
# С китайским прокси объявления открываются через него сразу, без попытки напрямую.
CHINA_PROXY = bool(os.environ.get("CHE168_PROXY_SERVER"))
USE_PROXY = os.environ.get("CHE168_USE_PROXY") == "1" or CHINA_PROXY
PROXY_SERVER = os.environ.get("CHE168_PROXY_SERVER") or os.environ.get("PROXY_SERVER") or ""
PROXY_USERNAME = (os.environ.get("CHE168_PROXY_USERNAME") if CHINA_PROXY else os.environ.get("PROXY_USERNAME")) or ""
PROXY_PASSWORD = (os.environ.get("CHE168_PROXY_PASSWORD") if CHINA_PROXY else os.environ.get("PROXY_PASSWORD")) or ""

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
            "brandid": attrs.get("brandid"),
            "specid": attrs.get("specid"),
            "dealerid": attrs["dealerid"],
            "name": attrs["carname"],
            "price_cny": round(price * 10_000) if price else None,
            "mileage_km": round(mileage * 10_000) if mileage is not None else None,
            "regdate": reg,
            "year": year,
            "image": ("https:" + img.group(1)) if img and img.group(1).startswith("//") else (img.group(1) if img else None),
        })
    return cards


def load_list(page, url: str) -> str | None:
    """Страница списка: до 3 попыток. Не ждём полной загрузки (скрипты рекламы и
    счётчиков из-за рубежа грузятся долго) — берём HTML, как только появились карточки."""
    for attempt in range(1, 4):
        try:
            page.goto(url, wait_until="commit", timeout=60_000)
            try:
                page.wait_for_selector("li[infoid]", timeout=45_000)
            except Exception:
                pass
            page.wait_for_timeout(random.randint(1500, 3000))
            content = page.content()
            if parse_cards(content) or attempt == 3:
                return content
            print(f"  попытка {attempt}: карточек пока нет — ещё раз")
        except Exception as error:
            print(f"  попытка {attempt}: {str(error).splitlines()[0][:150]}")
            if attempt == 3:
                return None
        human_pause(15, 30)
    return None


def collect(page, known: set, want: int) -> list[dict]:
    """Карточки 2020+ со страниц списка: новых — сколько нужно, известных — все встреченные."""
    seen, picked = set(), []
    new_count = 0
    for page_no in range(1, MAX_PAGES + 1):
        url = LIST_URL.format(page=page_no)
        content = load_list(page, url)
        if content is None:
            print(f"Список, страница {page_no}: не открылась за 3 попытки")
            break
        cards = parse_cards(content)
        if page_no == 1:
            save_debug("list_page_1.html", content)
        if not cards:
            print(f"Список, страница {page_no}: карточек нет — конец списка или блокировка")
            save_debug(f"list_page_{page_no}_empty.html", content)
            break
        fresh = [c for c in cards if c["infoid"] not in seen]
        learn_brands(cards)
        good = [c for c in fresh if c["year"] and c["year"] >= MIN_YEAR]
        # Машина без марки на сайте выглядит как «?» — такие не берём
        nameless = [c for c in good if not brand_model(c["name"], "", c.get("brandid"))[0]]
        for c in nameless:
            print(f"  пропуск — марка не определена: {c['name']} (номер марки {c.get('brandid')})")
        good = [c for c in good if c not in nameless]
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


class Degraded(Exception):
    """Страница открылась, но без блока «车辆档案» (характеристик) — che168 отдал урезанную версию."""


def fetch_detail(page, car: dict) -> str:
    """Объявление открываем в браузере: на прямые запросы che168 после нескольких штук
    отвечает урезанной страницей без характеристик."""
    url = f"https://www.che168.com/dealer/{car['dealerid']}/{car['infoid']}.html"
    resp = page.goto(url, wait_until="commit", timeout=60_000)
    try:
        page.wait_for_selector("span.item-name", timeout=12_000)
    except Exception:
        pass
    body = page.content()
    title = page.title()
    if resp and resp.status in (403, 429):
        raise Blocked(f"HTTP {resp.status}")
    if "安全验证" in title or ("item-name" not in body and "验证码" in body):
        # «二手车之家-安全验证» — капча Autohome: IP притормозили, повторы не помогут
        raise Blocked("капча «安全验证»")
    if "item-name" not in body:
        raise Degraded(title[:80])
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
    # Главное фото — первое в галерее (data-original 900×675); f_s_ — это миниатюры ленты
    gallery = re.findall(r"//[\w.]*autoimg\.cn/escimg/[^\"'\s]*?autohomecar__[^\"'\s]+?\.jpg", body)
    photos = []
    for url in dict.fromkeys(gallery):
        if "/110x110" not in url:
            photos = photo_candidates("https:" + url)
            break
    return {
        "spec": {k: v for k, v in spec.items() if v},
        "photos": photos,
    }


def guess_cc(name: str):
    """Объём по названию: «2.0T», «1.5L», «2.0G» или по коду мощности Audi/BMW/VW/Mercedes."""
    trim = re.split(r"\d{4}\s*款", name, maxsplit=1)[-1]
    m = re.search(r"(?<![\d.])(\d\.\d)\s*[A-Z]?(?![\d.])", trim)
    if m and 0.6 <= float(m.group(1)) <= 7.0:
        return round(float(m.group(1)) * 1000)
    m = re.search(r"\b(\d{2})\s*TFSI", trim)                      # Audi: 35 → 1.4T, 40/45 → 2.0T, 55 → 3.0T
    if m:
        return {30: 1400, 35: 1400, 40: 2000, 45: 2000, 50: 3000, 55: 3000}.get(int(m.group(1)))
    m = re.search(r"\b(\d{3})\s*TSI", trim)                       # VW Китай: 280 → 1.4T, 330/380 → 2.0T
    if m:
        n = int(m.group(1))
        return 1400 if n <= 300 else 2000 if n <= 380 else 2500
    m = re.search(r"(?:xDrive|sDrive)\s*(\d{2})|\b(\d{2})Li\b|\b[1-8](\d{2})Li?\b", trim)   # BMW: 20–30 → 2.0T, 35/40 → 3.0T
    if m:
        n = int(next(g for g in m.groups() if g))
        return 2000 if n <= 30 else 3000 if n <= 40 else 4400
    m = re.search(r"\b(?:[A-Z]{1,3}\s*)?(\d{3})\s*L?\b", trim)     # Mercedes: 200/260/300 → 2.0T, 350–450 → 3.0T
    if m and re.search(r"奔驰|Mercedes|\b[CEGS]\s*\d{3}|GL[ABCES]", name):
        n = int(m.group(1))
        return 2000 if n <= 300 else 3000 if n <= 450 else 4000
    return None


def spec_from_name(car: dict) -> dict:
    """Характеристики только по данным списка (когда объявление закрыто капчей):
    объём и тип двигателя — из названия («2.0T», «1.5L», «DM-i», «kWh»)."""
    name = car["name"]
    fuel = fuel_of(name, "")
    cc = guess_cc(name) if fuel != "Электро" else None
    spec = {
        "Лот": car["infoid"],
        "Первая регистрация": release(car["regdate"]),
        "Пробег": f"{car['mileage_km']:,} км".replace(",", " ") if car.get("mileage_km") is not None else None,
        "Модификация": latin_trim(name),
        "Тип топлива": fuel,
        "Рабочий объём цилиндров (см³)": str(cc) if cc else None,
    }
    return {k: v for k, v in spec.items() if v}


def photo_candidates(image: str | None) -> list[str]:
    """Крупные варианты того же снимка: сервер Autohome отдаёт размер по имени файла
    (1280×960 — если поддержит, 900×675 — самый крупный на странице объявления)."""
    if not image:
        return []
    m = re.search(r"/([^/]*?)autohomecar__([^/]+?\.jpg)", image)
    if not m:
        return [image]
    base, name = image[: m.start(1)], m.group(2)
    return [f"{base}{size}autohomecar__{name}" for size in
            ("1280x960_0_q87_c42_", "900x675_0_q87_c42_", "720x540_0_q87_c42_")] + [image]


# Номера марок Autohome, проверенные по реальным карточкам che168; остальные
# парсер выучивает на ходу по машинам, у которых марка есть в названии.
BRAND_IDS = {
    "1": "Volkswagen", "3": "Toyota", "8": "Ford", "12": "Hyundai", "14": "Honda", "15": "BMW", "25": "Geely",
    "26": "Chery", "27": "BAIC BJ", "33": "Audi", "34": "Alfa Romeo", "35": "Aston Martin", "36": "Mercedes-Benz",
    "38": "Buick", "39": "Bentley", "40": "Porsche", "42": "Ferrari", "46": "Jeep", "47": "Cadillac",
    "48": "Lamborghini", "49": "Land Rover", "50": "Lotus", "51": "Lincoln", "52": "Lexus", "54": "Rolls-Royce",
    "56": "MINI", "57": "Maserati", "58": "Mazda", "62": "Kia", "63": "Nissan", "65": "Subaru", "67": "Skoda",
    "68": "Mitsubishi", "70": "Volvo", "71": "Chevrolet", "73": "Infiniti", "75": "BYD", "76": "Changan",
    "77": "Great Wall", "82": "Trumpchi", "91": "Hongqi", "133": "Tesla", "181": "Haval", "284": "Nio",
    "345": "Li Auto", "371": "Genesis", "456": "Zeekr", "458": "Tank", "489": "Xiaomi", "502": "Avatr",
    "577": "Fangchengbao", "595": "Luxeed", "609": "AITO", "634": "Chery Fengyun",
}


def learn_brands(cards: list[dict]):
    """Номер марки → марка — по карточкам, где марка написана в названии."""
    for c in cards:
        make = extract_brand_model(c["name"])[0]
        if make and c.get("brandid"):
            BRAND_IDS[c["brandid"]] = make   # увиденное в названиях надёжнее таблицы


def brand_model(name: str, body: str, brandid: str | None = None):
    """Марка и модель из названия; если марки в нём нет («福克斯(进口)») — из «хлебных крошек» объявления."""
    make, model, _ = extract_brand_model(name)
    if make:
        return make, model
    if brandid and BRAND_IDS.get(brandid):
        # «Model Y 2021款», «福克斯(进口) 2018款» — марка по номеру, модель по названию
        from china_brand_map import _series_model
        series = re.split(r"\d{4}\s*款", name, maxsplit=1)[0]
        return BRAND_IDS[brandid], _series_model(re.sub(r"\(.*?\)|（.*?）", "", series))
    from china_brand_map import BRAND_MAP
    for crumb in re.findall(r"二手([^<>\s]{1,12})</a>", body):
        for key in sorted(BRAND_MAP, key=len, reverse=True):
            if crumb.startswith(key):
                make2, model2, _ = extract_brand_model(key + name)
                return make2, model2
    return None, None


# ---------- bn-auto ----------

def http_session():
    """Фото качаем напрямую: сервер картинок Autohome пускает и без прокси,
    а медленный (бесплатный) прокси не должен ломать загрузку фото."""
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": "https://www.che168.com/"})
    return s


def fetch_photo(session, urls):
    """Первое достаточно крупное фото (от 600 px в ширину); мелкое — только если другого нет."""
    from PIL import Image
    import io
    fallback = None
    for url in list(dict.fromkeys(u for u in urls if u))[:8]:
        try:
            resp = session.get(url, timeout=20)
            resp.raise_for_status()
            width = Image.open(io.BytesIO(resp.content)).width
        except Exception:
            continue
        data_url = compress_photo_to_data_url(resp.content)
        if not data_url:
            continue
        if width >= 600:
            return data_url
        fallback = fallback or data_url
    return fallback


def fetch_known() -> set:
    if not BN_AUTO_URL or not BN_AUTO_IMPORT_TOKEN:
        return set()
    try:
        resp = requests.get(f"{BN_AUTO_URL}/api/live-listings/known", params={"source": "che168"},
                            headers={"Authorization": f"Bearer {BN_AUTO_IMPORT_TOKEN}"}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items")
        if items is None:
            ids = {str(i) for i in data.get("ids") or []}
        else:
            # Машины, сохранённые без двигателя (урезанная страница), перезагружаем заново
            # …и машины с мелким фото (миниатюрой) — перезагружаем с крупным
            ids = {str(i["id"]) for i in items
                   if i.get("has_engine") and i.get("make") and (i.get("photo_kb") or 999) >= 45}
            if len(items) > len(ids):
                print(f"Без двигателя, марки или с мелким фото на сайте: {len(items) - len(ids)} — загрузим заново")
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

def new_context(browser, use_proxy: bool):
    proxy = None
    if use_proxy and PROXY_SERVER:
        proxy = {k: v for k, v in {"server": PROXY_SERVER, "username": PROXY_USERNAME, "password": PROXY_PASSWORD}.items() if v}
        print(f"Используем прокси: {PROXY_SERVER}")
    context = browser.new_context(user_agent=UA, viewport={"width": 1440, "height": 900}, locale="zh-CN",
                                  timezone_id="Asia/Shanghai", proxy=proxy,
                                  extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6"})
    context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
    return context


def main():
    known = fetch_known()
    session = http_session()
    listings = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        context = new_context(browser, USE_PROXY)
        page = context.new_page()
        cars = collect(page, known, TOTAL)
        if not cars and USE_PROXY:
            # Прокси не отвечает (бесплатные быстро умирают) — список пробуем напрямую
            print("Через прокси список не получен — пробуем напрямую")
            context.close()
            context = new_context(browser, False)
            page = context.new_page()
            cars = collect(page, known, TOTAL)
        elif not cars and PROXY_SERVER:
            # Напрямую che168 не отдал список — пробуем через прокси
            print("Напрямую список не получен — пробуем через прокси")
            context.close()
            context = new_context(browser, True)
            page = context.new_page()
            cars = collect(page, known, TOTAL)
        print(f"Отобрано: {len(cars)} (уже на сайте: {sum(c['infoid'] in known for c in cars)}), марок по номерам: {len(BRAND_IDS)}")

        done = failed = degraded = degraded_saved = from_list = 0
        on_proxy = USE_PROXY
        list_only = False   # объявления закрыты капчей — дальше только данные списка
        breaks = random.randint(20, 30)

        def add(car, url, body=None, d=None):
            nonlocal done, from_list
            if d is None:
                d = {"spec": spec_from_name(car), "photos": []}
                from_list += 1
            make, model = brand_model(car["name"], body or "", car.get("brandid"))
            listings.append({
                "external_id": car["infoid"], "make": make, "model": model, "title": car["name"],
                "year": car["year"], "mileage_km": car["mileage_km"], "price_value": car["price_cny"],
                "photo_url": fetch_photo(session, d["photos"] + photo_candidates(car.get("image"))),
                "spec": d["spec"] or None, "source_url": url,
            })
            done += 1
            src = "из списка" if body is None else "из объявления"
            print(f"[{done}] {make or '?'} {model or ''} {car['year']} — {car['price_cny']} ¥, характеристик: {len(d['spec'])} ({src})")

        for car in cars:
            url = f"https://www.che168.com/dealer/{car['dealerid']}/{car['infoid']}.html"
            if car["infoid"] in known:
                listings.append({"external_id": car["infoid"], "price_value": car["price_cny"],
                                 "mileage_km": car["mileage_km"], "source_url": url})
                continue
            if list_only:
                add(car, url)
                continue
            try:
                try:
                    body = fetch_detail(page, car)
                except Degraded as first:
                    if degraded_saved < 3:
                        save_debug(f"detail_degraded_{car['infoid']}.html", page.content())
                        degraded_saved += 1
                    print(f"[{car['infoid']}] страница без характеристик ({first}) — пауза и ещё попытка")
                    human_pause(10, 20)
                    body = fetch_detail(page, car)
            except Degraded:
                degraded += 1
                print(f"[{car['infoid']}] снова без характеристик — берём данные из списка")
                add(car, url)
                continue
            except Blocked as reason:
                if not on_proxy and PROXY_SERVER:
                    print(f"che168 показал {reason} — переключаемся на прокси")
                    context.close()
                    context = new_context(browser, True)
                    page = context.new_page()
                    on_proxy = True
                    try:
                        body = fetch_detail(page, car)
                    except Exception as again:
                        print(f"Через прокси тоже не пускает ({again}) — остальные машины по данным списка")
                        list_only = True
                        add(car, url)
                        continue
                else:
                    print(f"che168 показал {reason} — остальные машины по данным списка")
                    list_only = True
                    add(car, url)
                    continue
            except Exception as error:
                failed += 1
                print(f"[{car['infoid']}] не открылось: {str(error).splitlines()[0][:150]} — берём данные из списка")
                add(car, url)
                continue
            if degraded_saved < 5 and done < 2:
                save_debug(f"detail_{car['infoid']}.html", body)
            add(car, url, body, parse_detail(body, car))
            human_pause(3, 7)
            if done >= breaks:
                human_pause(25, 45)
                breaks = done + random.randint(20, 30)
        browser.close()

    print(f"Итого: новых {sum('spec' in x for x in listings)} (из них только по списку {from_list}), "
          f"уже на сайте {sum('spec' not in x for x in listings)}, урезанных страниц {degraded}, ошибок {failed}")
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump([{k: v for k, v in x.items() if k != "photo_url"} for x in listings], f, ensure_ascii=False, indent=2)
    push(listings)


if __name__ == "__main__":
    main()
