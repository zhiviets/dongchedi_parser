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
from datetime import date

import requests
from playwright.sync_api import sync_playwright

from china_brand_map import extract_brand_model
from weekly_scraper import compress_photo_to_data_url

# Сколько новых машин за прогон (0 — само: до FILL_TARGET на сайте, потом раз в неделю)
TOTAL = int(os.environ.get("CHE168_TOTAL") or "0")
# Заполнение каталога: пока машин с полной информацией на сайте меньше FILL_TARGET — каждый
# прогон добавляет до FILL_PER_RUN новых порциями по BATCH с паузой BATCH_PAUSE (1–2 мин) и в
# конце запускает следующий. Потом — обновление два раза в неделю (UPDATE_DAYS, 0 — понедельник,
# первый прогон дня): до UPDATE_NEW новых порциями по UPDATE_BATCH с паузой UPDATE_PAUSE минут;
# в остальное время прогон сразу заканчивается.
FILL_TARGET = int(os.environ.get("CHE168_FILL_TARGET") or "5500")
FILL_PER_RUN = int(os.environ.get("CHE168_FILL_PER_RUN") or "1000")
# Через сколько минут от начала прогона перестать открывать объявления и отправить собранное
# (предел GitHub Actions — 6 часов)
RUN_MINUTES = float(os.environ.get("CHE168_RUN_MINUTES") or "320")
UPDATE_DAYS = {int(d) for d in (os.environ.get("CHE168_UPDATE_DAYS") or "1,4").split(",") if d.strip()}
UPDATE_NEW = int(os.environ.get("CHE168_UPDATE_NEW") or "600")
UPDATE_BATCH = int(os.environ.get("CHE168_UPDATE_BATCH") or "150")
UPDATE_PAUSE = float(os.environ.get("CHE168_UPDATE_PAUSE") or "30")
# Разнообразие: не больше стольких машин одной модели за прогон — отдельно до 160 л.с. и мощнее,
# чтобы доля 75/25 сохранялась
PER_MODEL_RUN = {"le160": int(os.environ.get("CHE168_PER_MODEL_LE160") or "3"),
                 "other": int(os.environ.get("CHE168_PER_MODEL_OTHER") or "2")}
# Доля машин до 160 л.с. (проходных по утильсбору), остальные — любой мощности
SHARE_160 = float(os.environ.get("CHE168_SHARE_160") or "0.75")
MIN_YEAR = int(os.environ.get("CHE168_MIN_YEAR") or "2010")
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


HYBRID_MARK = re.compile(r"插电|插混|增程|混动|DM-?i|DM-?p|EM-?[iP]\b|PHEV|HEV|DHT|油电|双擎|iDD|Hi[·.]?[PX]\b", re.I)


# Марки и модели, где в Китае продаются только электромобили (в названии объявления «EV» часто нет)
EV_ONLY = re.compile(r"特斯拉|Tesla|蔚来|小鹏|埃安|AION|极氪|ZEEKR|MINI ?EV|欧拉|几何|哪吒|小米SU7|小米YU7|理想\s?MEGA|ID\.\s?\d", re.I)


def fuel_of(name: str, engine: str) -> str:
    text = f"{name} {engine}"
    ev = re.search(r"纯电|电动|kWh|\bEV\b", text, re.I) or EV_ONLY.search(text)
    # «宋PLUS新能源 2023款 EV 605KM»: запас хода от 350 км и нет объёма мотора — тоже электромобиль
    if not ev and not re.search(r"(?<![\d.])\d\.\d\s*[LT]?\b", text):
        rng = re.search(r"(\d{3,4})\s*km\b", text, re.I)
        ev = rng and int(rng.group(1)) >= 350
    if ev and not HYBRID_MARK.search(text):
        return "Электро"
    # Li Auto L-серии и ONE, AITO (问界) без пометки «纯电» — последовательные гибриды
    if re.search(r"增程|理想\s?(?:L\d|ONE)|问界", text, re.I):
        return "Последовательный гибрид (увеличенный запас хода)"
    if HYBRID_MARK.search(text) or re.search(r"e:HEV|锐·混动", text, re.I):
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
        # Фото ниже первого экрана подгружаются по прокрутке: в src заглушка, снимок — в src2
        img = re.search(r'<img[^>]+?\b(?:src2|data-original|src)="([^"]+autohomecar__[^"]+)"', chunk)
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


def load_list(page, url: str, attempts: int = 3, wait_ms: int = 45_000) -> str | None:
    """Страница списка: до 3 попыток. Не ждём полной загрузки (скрипты рекламы и
    счётчиков из-за рубежа грузятся долго) — берём HTML, как только появились карточки."""
    for attempt in range(1, attempts + 1):
        try:
            page.goto(url, wait_until="commit", timeout=60_000)
            try:
                page.wait_for_selector("li[infoid]", timeout=wait_ms)
            except Exception:
                pass
            page.wait_for_timeout(random.randint(1500, 3000))
            content = page.content()
            if parse_cards(content) or attempt == attempts:
                return content
            print(f"  попытка {attempt}: карточек пока нет — ещё раз")
        except Exception as error:
            print(f"  попытка {attempt}: {str(error).splitlines()[0][:150]}")
            if attempt == attempts:
                return None
        human_pause(15, 30)
    return None


# Фильтр «排量» (объём) на che168: машины до 160 л.с. — в основном до 2.0 л,
# а в общем списке первыми идут премиум и электромобили
DISPLACE_FILTERS = {"2": "1.1–1.6 л", "3": "1.7–2.0 л", "1": "до 1.0 л"}


def filter_url(page, val: str):
    """Адрес списка с фильтром объёма (шаблон с {page}) или None; False — список не открылся.

    Схема адресов che168 не документирована, поэтому отмечаем галочку фильтра
    на самой странице и запоминаем, куда che168 перешёл.
    """
    base = LIST_URL.format(page=1)
    if load_list(page, base) is None:
        return False
    try:
        with page.expect_navigation(wait_until="commit", timeout=30_000):
            page.evaluate("""v => {
                const el = document.querySelector('#btndisplacegroup input[val="' + v + '"]');
                if (!el) throw new Error('галочки фильтра нет на странице');
                el.click();
            }""", val)
    except Exception as error:
        print(f"  фильтр объёма {DISPLACE_FILTERS[val]}: {str(error).splitlines()[0][:150]}")
        return None
    url = page.url.split("#")[0].split("?")[0]
    m = re.search(r"sp\d*(?=ex)", url)
    if url.rstrip("/") == base.rstrip("/") or not m:
        print(f"  фильтр объёма {DISPLACE_FILTERS[val]}: адрес не понятен ({url})")
        return None
    return url[:m.start()] + "sp{page}" + url[m.end():]


def collect(page, known: set, want: int) -> list[dict]:
    """Карточки 2020+ со страниц списка: новых — сколько нужно (75% до 160 л.с.),
    известных — все встреченные до 160 л.с. и не больше трети от них остальных.
    Машины, мощность которых не оценить, не берём. Сначала листаем списки с
    фильтром объёма до 2.0 л, потом — весь список."""
    seen, picked = set(), []
    quota = {"le160": round(want * SHARE_160)}
    quota["other"] = want - quota["le160"]
    new_count = {"le160": 0, "other": 0}
    known_count = {"le160": 0, "other": 0}
    ratio = (1 - SHARE_160) / SHARE_160 if SHARE_160 else 0

    sources = []
    for val, label in DISPLACE_FILTERS.items():
        url = filter_url(page, val)
        if url is False:
            break   # список не открывается (прокси) — main() попробует другой путь
        if url:
            print(f"Фильтр объёма {label}: {url}")
            sources.append((f"объём {label}", url))
        human_pause(3, 7)
    sources.append(("весь список", LIST_URL))

    def full():
        return all(new_count[b] >= quota[b] for b in quota)

    for label, template in sources:
        for page_no in range(1, MAX_PAGES + 1):
            content = load_list(page, template.format(page=page_no))
            if content is None:
                print(f"[{label}] страница {page_no}: не открылась за 3 попытки")
                break
            cards = parse_cards(content)
            if page_no == 1 and not picked:
                save_debug("list_page_1.html", content)
            if not cards:
                print(f"[{label}] страница {page_no}: карточек нет — конец списка или блокировка")
                save_debug(f"list_page_{page_no}_empty.html", content)
                break
            learn_brands(cards)
            fresh = [c for c in cards if c["infoid"] not in seen]
            good = [c for c in fresh if c["year"] and c["year"] >= MIN_YEAR]
            # Машина без марки на сайте выглядит как «?» — такие не берём
            nameless = [c for c in good if not brand_model(c["name"], "", c.get("brandid"))[0]]
            for c in nameless:
                print(f"  пропуск — марка не определена: {c['name']} (номер марки {c.get('brandid')})")
            good = [c for c in good if c not in nameless]
            for c in fresh:
                seen.add(c["infoid"])
            for c in good:
                c["power"] = power_class(c["name"], brand_model(c["name"], "", c.get("brandid"))[0])
                if not c["power"]:
                    continue   # бензин/дизель без объёма в названии — мощность не оценить, не берём
                if c["infoid"] in known:
                    # Уже на сайте: мощные обновляем, только пока их не больше трети от «до 160»
                    if c["power"] == "le160" or known_count["other"] < known_count["le160"] * ratio:
                        picked.append(c)
                        known_count[c["power"]] += 1
                elif new_count[c["power"]] < quota[c["power"]]:
                    picked.append(c)
                    new_count[c["power"]] += 1
            print(f"[{label}] страница {page_no}: карточек {len(cards)}, с {MIN_YEAR} г. {len(good)} "
                  f"(мощность оценена у {sum(bool(c['power']) for c in good)}), новых набрано: "
                  f"до 160 л.с. {new_count['le160']}/{quota['le160']}, любой мощности {new_count['other']}/{quota['other']}")
            if full():
                break
            human_pause(3, 7)
        if full():
            break
    # Список кончился раньше, чем набралось «до 160» — мощных оставляем не больше трети от них
    extra = new_count["other"] - round(new_count["le160"] * ratio)
    if extra > 0:
        drop = {id(c) for c in [c for c in picked if c["infoid"] not in known and c["power"] == "other"][-extra:]}
        picked = [c for c in picked if id(c) not in drop]
        print(f"Машин до 160 л.с. нашлось мало — мощных новых убрано {extra}, чтобы их было не больше 25%")
    return picked


# ---------- все модели ----------
# Хотя бы по одной машине каждой модели: обходим страницы марок (/china/aodi/)
# и моделей (/china/aodi/aodia4l/), затем выбираем — см. pick_models().
ALL_MODELS = os.environ.get("CHE168_ALL_MODELS", "1") == "1"
# Сколько минут обходить марки и модели (остальное время — фото и отправка на сайт)
SCAN_MINUTES = float(os.environ.get("CHE168_SCAN_MINUTES") or "120")
# Порция машин между паузами и длина паузы в минутах: 100 машин — отправка на сайт — пауза
BATCH = int(os.environ.get("CHE168_BATCH") or "100")
BATCH_PAUSE = float(os.environ.get("CHE168_BATCH_PAUSE") or "1.5")
BRAND_LINK_RE = re.compile(r'<a[^>]+href="/china/([a-z][a-z0-9]*)/#pvareaid=105866[^"]*"[^>]*>([^<]{1,20})</a>')


def series_key(name: str) -> str:
    """Модель по названию карточки: «奥迪A4L 2022款 40 TFSI» → «奥迪A4L»."""
    return re.split(r"\d{4}\s*款", name, maxsplit=1)[0].strip() or name


SCAN_WORKERS = int(os.environ.get("CHE168_SCAN_WORKERS") or "3")
SCAN_STATE = {"closed": False}   # обход остановлен: che168 закрыл IP (капча или страницы не открываются)


def _norm(text: str) -> str:
    return re.sub(r"[\s·\-()（）]", "", text or "").lower()


def is_real_list(html: str) -> bool:
    """Настоящая страница списка che168 (с карточками или честно пустая) — в отличие от
    заглушки защиты от ботов: у настоящей есть фильтры и список марок."""
    return bool(parse_cards(html)) or "moredisplace" in html or bool(BRAND_LINK_RE.search(html))


class ScanGate:
    """Общий темп обхода для всех потоков. che168 ограничивает частоту запросов волнами: 5–20 минут все страницы
    не открываются, потом снова открываются. Раньше потоки долбили его всё это время, и каждая неоткрывшаяся
    страница пропадала (за прогон — 360 моделей из 3 900). Теперь: COOL_AFTER неудач подряд — все потоки ждут
    3–5 мин (дальше дольше, до 12), темп вдвое медленнее; 120 удачных страниц — снова быстрее."""
    COOL_AFTER = int(os.environ.get("CHE168_COOL_AFTER") or "6")

    def __init__(self):
        import threading
        self.lock = threading.Lock()
        self.until = 0.0
        self.slow = 1.0
        self.fails = 0
        self.oks = 0
        self.cools = 0          # пауз подряд без удачной страницы между ними
        self.cool_total = 0

    def wait(self):
        while True:
            left = self.until - time.time()
            if left <= 0:
                break
            time.sleep(min(left, 5))
        time.sleep(random.uniform(0.6, 1.8) * self.slow)

    def result(self, ok: bool):
        with self.lock:
            if ok:
                self.fails = 0
                self.cools = 0
                self.oks += 1
                if self.slow > 1 and self.oks >= 120:
                    self.slow, self.oks = max(1.0, self.slow / 2), 0
                return
            self.fails += 1
            self.oks = 0
            if self.fails >= self.COOL_AFTER and time.time() >= self.until:
                pause = min(12.0, random.uniform(3, 5) * (1.5 ** self.cools)) * 60
                self.until = time.time() + pause
                self.cools += 1
                self.cool_total += 1
                self.slow = min(self.slow * 2, 6)
                self.fails = 0
                print(f"  che168 ограничил запросы — пауза {pause / 60:.1f} мин, дальше медленнее (×{self.slow:g})")

    @property
    def closed(self) -> bool:
        # 4 паузы подряд, а страницы так и не открываются — che168 закрыл этот IP надолго
        return self.cools >= 4 and self.fails >= self.COOL_AFTER


GATE = ScanGate()


def browser_fetcher():
    """Страницы списка — у каждого потока свой браузер. Простым запросам che168 отвечает
    заглушкой защиты от ботов (200, но без карточек), браузер пропускает. Внутри браузера
    сначала быстрый запрос page.request (с куками, которые браузер получил), и только если
    пришла заглушка — полноценная загрузка страницы. Возвращает (get, close_all)."""
    import threading
    from playwright.sync_api import sync_playwright
    local = threading.local()
    stats = {"request": 0, "browser": 0}

    def page_for_thread():
        if not hasattr(local, "page"):
            local.pw = sync_playwright().start()
            local.browser = local.pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
            local.page = new_context(local.browser, False).new_page()
            load_list(local.page, LIST_URL.format(page=1), attempts=1, wait_ms=20_000)   # куки защиты
        return local.page

    def get(url):
        page = page_for_thread()
        GATE.wait()
        try:
            resp = page.request.get(url, timeout=30_000, headers={"Referer": "https://www.che168.com/china/list/"})
            html = decode_html(resp.body(), resp.headers.get("content-type", ""))
            if re.search(r"<title>[^<]*安全验证", html):
                return "blocked"
            if resp.status == 200 and is_real_list(html):
                stats["request"] += 1
                return html
        except Exception:
            pass
        stats["browser"] += 1
        html = load_list(page, url, attempts=1, wait_ms=15_000)
        if html and re.search(r"<title>[^<]*安全验证", html):
            return "blocked"
        return html if html and is_real_list(html) else None

    def close_all(pool, workers):
        """Браузеры закрываем в их же потоках: по задаче на поток (барьер не даёт одному
        потоку взять две)."""
        barrier = threading.Barrier(workers)

        def close_one():
            if hasattr(local, "browser"):
                try:
                    local.browser.close()
                    local.pw.stop()
                except Exception:
                    pass
            try:
                barrier.wait(timeout=60)
            except threading.BrokenBarrierError:
                pass

        for f in [pool.submit(close_one) for _ in range(workers)]:
            f.result()

    get.stats = stats
    return get, close_all


MIN_SIMILAR = 4


def price_stats(prices: list) -> dict:
    """{n, lo, mid, hi}: от 10 цен — 10-й и 90-й процентили, иначе самая низкая и самая высокая."""
    p = sorted(prices)
    at = lambda q: p[round((len(p) - 1) * q)]
    wide = len(p) >= 10
    return {"n": len(p), "lo": at(0.1) if wide else p[0], "mid": at(0.5), "hi": at(0.9) if wide else p[-1]}


PRICES = {}   # группа похожих → цены объявлений che168 (собираются при обходе)
# Марка и модель на сайте (со страницы объявления) → как в списке che168 (по названию) — по машинам сайта,
# встреченным в обходе: названия могут отличаться («Sagitar» и «Jetta Sagitar»)
SITE_TO_LIST = {}
SITE_ITEMS = {}   # машины сайта (/known) — для соответствия названий
STATS_DAYS = int(os.environ.get("CHE168_STATS_DAYS") or "10")
MIX = {"limits": None}   # лимиты разнообразия [на модель-год, на модель] — с сайта (/known)


def similar_prices(make, model, year, cc=None, specid=None) -> list:
    """Цены похожих: та же комплектация Autohome (specid); мало (меньше MIN_SIMILAR) — шире: модель, год и
    объём; модель и год; соседние годы ±1, потом ±2."""
    if not (make and model and year):
        return []
    levels = ([[("s", specid)]] if specid and specid != "0" else []) + ([[("c", make, model, year, cc)]] if cc else []) + [
        [("y", make, model, year)],
        [("y", make, model, year + d) for d in (-1, 0, 1)],
        [("y", make, model, year + d) for d in range(-2, 3)],
    ]
    for keys in levels:
        prices = [p for k in keys for p in PRICES.get(k, [])]
        if len(prices) >= MIN_SIMILAR:
            return prices
    return []


def attach_price_stats(cards: list):
    """Шкала цены на сайте: цены похожих объявлений che168 → car["price_stats"] и stats_key (группа)."""
    for c in cards:
        make, model = brand_model(c["name"], "", c.get("brandid"))[:2]
        c["_mm"] = (make, model)
        if c.get("price_cny") and make and model and c.get("year"):
            if c.get("specid") and c["specid"] != "0":
                PRICES.setdefault(("s", c["specid"]), []).append(c["price_cny"])
            PRICES.setdefault(("c", make, model, c["year"], guess_cc(c["name"])), []).append(c["price_cny"])
            PRICES.setdefault(("y", make, model, c["year"]), []).append(c["price_cny"])
    done = 0
    for c in cards:
        make, model = c["_mm"]
        prices = similar_prices(make, model, c.get("year"), guess_cc(c["name"]), c.get("specid"))
        if prices:
            c["price_stats"] = price_stats(prices)
            c["stats_key"] = f"{make}|{model}|{c['year']}"
            done += 1
    print(f"Статистика цен: у {done} из {len(cards)} машин {MIN_SIMILAR}+ похожих объявлений")


def stale_stats(items: dict, seen: set) -> list[dict]:
    """Машины сайта, не встреченные в обходе, у которых шкалы нет или она старше STATS_DAYS дней, — свежая
    статистика по ценам, собранным при обходе (по марке, модели и году как на сайте)."""
    out, lack = [], 0
    for vid, i in items.items():
        if vid in seen or not i.get("published") or not i.get("url"):
            continue
        if i.get("has_gauge") and i.get("stats_days") is not None and i["stats_days"] < STATS_DAYS:
            continue
        try:
            year = int(i.get("year") or 0)
        except ValueError:
            year = 0
        make, model = SITE_TO_LIST.get((i.get("make"), i.get("model")), (i.get("make"), i.get("model")))
        prices = similar_prices(make, model, year)
        if not prices:
            lack += 1
            if lack <= 15:
                print(f"  нет похожих: {i.get('make')} {i.get('model')} {year} (в списке che168 — {make} {model})")
            continue
        out.append({"external_id": vid, "source_url": i["url"], "price_stats": price_stats(prices),
                    "stats_key": f"{make}|{model}|{year}"})
    print(f"Шкала цены машинам сайта, не встреченным в обходе: обновлена у {len(out)}, нет похожих у {lack} "
          f"(соответствий названий сайт → che168: {len(SITE_TO_LIST)})")
    return out


# Китай: новые машины добавляем и дальше (каждая — сразу со шкалой цены), машинам сайта без шкалы цены ищем
# параллельно (страницы их моделей открываются первыми); 1 — сначала шкала всем машинам сайта, потом новые
GAUGE_FIRST = (os.environ.get("CHE168_GAUGE_FIRST") or "0") == "1"
GAUGE_SEARCH = {"done": False}   # поиск цен машинам сайта без шкалы прошёл целиком (без капчи и не по времени)
GAUGE_FIRST_MAX = int(os.environ.get("CHE168_GAUGE_FIRST_MAX") or "50")


def lacking_gauge(items: dict) -> int:
    return sum(1 for i in items.values() if i.get("published") and not i.get("blocked") and not i.get("has_gauge"))


def stats_due(items: dict) -> bool:
    """Прогон ради шкалы (новых машин не надо): у CHE168_STATS_MIN+ машин сайта шкалы нет или она старше STATS_DAYS."""
    pub = [i for i in items.values() if i.get("published")]
    stale = sum(1 for i in pub if not i.get("has_gauge") or i.get("stats_days") is None or i["stats_days"] >= STATS_DAYS)
    need = stale >= int(os.environ.get("CHE168_STATS_MIN") or "300") or (GAUGE_FIRST and lacking_gauge(items) > GAUGE_FIRST_MAX)
    print(f"Шкала цены: без неё или старше {STATS_DAYS} дн. — {stale} из {len(pub)} машин"
          + (" — обход ради шкалы" if need else ""))
    return need


def scan_models(page, known: set, touched: list | None = None):
    """Карточки всех моделей: {модель: [машины]}. False — список не открылся (прокси), None — марок не нашли.

    Сначала страницы всех марок (/china/aodi/), потом страницы моделей (/china/aodi/aodia4l/),
    машин которых на странице марки не было, — по кругу по маркам, пока не выйдет время.
    Страницы качаются в CHE168_SCAN_WORKERS потоков."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    started = time.time()
    deadline = started + SCAN_MINUTES * 60
    html = load_list(page, LIST_URL.format(page=1))
    if html is None:
        return False
    brands = list(dict.fromkeys(BRAND_LINK_RE.findall(html)))
    if not brands:
        save_debug("brands_missing.html", html)
        print("На странице che168 не нашлось списка марок")
        return None
    # Сначала марки, которые мы знаем (их больше всего продаётся), потом редкие
    known_names = set(BRAND_IDS.values())
    brands.sort(key=lambda b: extract_brand_model(b[1] + " 2020款")[0] not in known_names)
    print(f"Марок на che168: {len(brands)} — обходим марки, потом их модели "
          f"(до {SCAN_MINUTES:.0f} мин, потоков {SCAN_WORKERS})")

    GATE.__init__()
    get, close_all = browser_fetcher()
    pool = ThreadPoolExecutor(SCAN_WORKERS)
    groups, seen, price_seen = {}, set(), []   # price_seen — все карточки с ценой: для шкалы цены
    stats = {"ok": 0, "empty": 0, "failed": 0, "blocked": 0}
    saved = set()

    def add_cards(content):
        cards = parse_cards(content)
        learn_brands(cards)
        for c in cards:
            if c["infoid"] in seen or not c["year"] or c["year"] < MIN_YEAR:
                continue
            seen.add(c["infoid"])
            price_seen.append(c)
            if c["infoid"] in known:
                # Уже на сайте с полной информацией — только отметка «ещё в продаже»
                if touched is not None:
                    touched.append(c)
                    info = SITE_ITEMS.get(str(c["infoid"])) or {}
                    if info.get("make") and info.get("model"):
                        SITE_TO_LIST[(info["make"], info["model"])] = tuple(brand_model(c["name"], "", c.get("brandid"))[:2])
                continue
            make, model = brand_model(c["name"], "", c.get("brandid"))
            if not model:
                continue   # без модели на сайт не попадёт — не занимаем место в отборе
            c["power"] = power_class(c["name"], make) if make else None
            if c["power"]:
                groups.setdefault(series_key(c["name"]), []).append(c)
        return cards

    def run(jobs, label, on_page, retry_round=False):
        """jobs — [(url, данные)]; на каждую скачанную страницу — on_page(html, данные). Страницы, которые не
        открылись (che168 ограничил запросы), — ещё раз в конце, после паузы (GATE)."""
        done = 0
        failed_jobs = []
        futures = {pool.submit(get, url): (url, extra) for url, extra in jobs}
        for fut in as_completed(futures):
            url, extra = futures[fut]
            try:
                got = fut.result()
            except Exception as error:
                print(f"  {label}: {url} — {str(error).splitlines()[0][:120]}")
                got = None
            done += 1
            GATE.result(bool(got) and got != "blocked")
            if got == "blocked" or not got:
                stats["blocked" if got == "blocked" else "failed"] += 1
                failed_jobs.append((url, extra))
            else:
                if not parse_cards(got):
                    stats["empty"] += 1
                else:
                    stats["ok"] += 1
                    if label not in saved:   # образец страницы — для доработки разбора
                        saved.add(label)
                        save_debug(f"sample_{label}.html", got)
                on_page(got, extra)
            if done % 50 == 0:
                print(f"  {label}: {done}/{len(jobs)}, моделей с машинами {len(groups)}, "
                      f"{(time.time() - started) / 60:.0f} мин; страниц с машинами {stats['ok']}, "
                      f"пустых {stats['empty']}, ошибок {stats['failed']}, проверок {stats['blocked']}, "
                      f"пауз {GATE.cool_total}")
            if time.time() > deadline or GATE.closed:
                for x in futures:
                    x.cancel()
                SCAN_STATE["closed"] = GATE.closed
                why = "che168 закрыл доступ надолго (страницы не открываются после 4 пауз)" if GATE.closed \
                    else "время обхода вышло"
                print(f"  {label}: {why} — обработано {done} из {len(jobs)}")
                return False
        if failed_jobs and not retry_round:
            print(f"  {label}: не открылось {len(failed_jobs)} страниц — ещё раз")
            return run(failed_jobs, label + " (повтор)", on_page, retry_round=True)
        return True

    add_cards(html)
    series_by_brand = {}

    def on_brand(content, brand):
        slug, _ = brand
        if not add_cards(content):
            return
        # Модели марки — ссылки /china/<марка>/<модель>/ с названием
        series_by_brand[slug] = list(dict.fromkeys(
            (sl, name.strip()) for sl, name in
            re.findall(rf'<a[^>]+href="/china/{slug}/([a-z][a-z0-9]*)/[^"]*"[^>]*>([^<]{{1,30}})</a>', content)
            if name.strip()))

    # Сначала марки машин сайта без шкалы цены и сразу страницы их моделей (цены похожих им нужны в первую
    # очередь; к концу долгого обхода che168 начинает показывать проверку), потом остальные марки
    lacking_items = [i for i in SITE_ITEMS.values()
                     if i.get("published") and not i.get("blocked") and not i.get("has_gauge")]
    lacking = [_norm(i.get("text") or "") for i in lacking_items]
    lacking_makes = {i.get("make") for i in lacking_items if i.get("make")}
    first = [b for b in brands if extract_brand_model(b[1] + " 2020款")[0] in lacking_makes]
    rest = [b for b in brands if b not in first]
    ok = run([(f"https://www.che168.com/china/{slug}/", (slug, name)) for slug, name in first],
             "марки машин без шкалы", on_brand) if first else True
    # Страница модели, название которой есть в названии машины сайта без шкалы; больше таких машин — раньше
    priority = [(f"https://www.che168.com/china/{slug}/{sl}/", name) for slug, _ in first if slug in series_by_brand
                for sl, name in series_by_brand[slug] if len(_norm(name)) >= 2]
    hits = {url: sum(_norm(name) in t for t in lacking) for url, name in priority}
    priority = sorted((x for x in priority if hits[x[0]]), key=lambda x: -hits[x[0]])
    if ok and priority and time.time() < deadline:
        print(f"Модели машин сайта без шкалы цены: {len(priority)} страниц (марок {len(first)}) — открываем первыми")
        ok = run(priority, "модели без шкалы", lambda content, name: add_cards(content))
    # Марки и модели машин без шкалы обойдены целиком — кому цена не нашлась, тех в конце прогона удаляем
    # …и страницы правда открывались (прокси или che168 не отдавали страниц — цены не искали, удалять нельзя)
    GAUGE_SEARCH["done"] = bool(ok and time.time() < deadline and stats["ok"] >= 20)
    if ok and time.time() < deadline:
        ok = run([(f"https://www.che168.com/china/{slug}/", (slug, name)) for slug, name in rest], "марки", on_brand)
    with_cars = [b for b in brands if b[0] in series_by_brand]
    print(f"Марки: с машинами {len(with_cars)}, моделей с машинами {len(groups)}, "
          f"ссылок на модели {sum(map(len, series_by_brand.values()))}")
    # Модели, машин которых ещё не встретили, — по кругу по маркам (популярные марки первыми)
    keys = [_norm(k) for k in groups]
    queues = [[(f"https://www.che168.com/china/{slug}/{sl}/", name) for sl, name in series_by_brand[slug]
               if not any(_norm(name) in k or k in _norm(name) for k in keys)]
              for slug, _ in with_cars]
    jobs = []
    while any(queues):
        for q in queues:
            if q:
                jobs.append(q.pop(0))
    done_urls = {url for url, _ in priority}
    jobs = [j for j in jobs if j[0] not in done_urls]
    if ok and jobs and time.time() < deadline:
        run(jobs, "модели", lambda content, name: add_cards(content))
    close_all(pool, SCAN_WORKERS)
    pool.shutdown(wait=True)
    attach_price_stats(price_seen)
    print(f"Обход за {(time.time() - started) / 60:.0f} мин: моделей с машинами {len(groups)}, "
          f"машин {sum(map(len, groups.values()))}; страниц с машинами {stats['ok']}, пустых {stats['empty']}, "
          f"ошибок {stats['failed']}, проверок {stats['blocked']}; запросом {get.stats['request']}, "
          f"через загрузку страницы {get.stats['browser']}")
    return groups


# Доли по годам: 40% — 2022–2024, 15% — 2025–2026, 25% — 2017–2021, 20% — 2010–2016 (массовые модели прошлых
# лет тоже нужны каталогу; порядок — приоритет)
YEAR_BANDS = [("2022–2024", 2022, 2024, 0.40), ("2025–2026", 2025, 2026, 0.15), ("2017–2021", 2017, 2021, 0.25),
              ("2010–2016", 2010, 2016, 0.20)]


def year_band(year):
    return next((name for name, lo, hi, _ in YEAR_BANDS if year and lo <= year <= hi), None)


KINDS = ("le160", "other")


def catalog_have(items) -> dict:
    """Состав каталога: {(класс мощности, годы): машин} — по опубликованным полным машинам с
    сайта (мощность и «электро» — как их считает сайт)."""
    have = {}
    for i in items:
        if not (i.get("complete") and i.get("published")):
            continue
        power = i.get("power") or 0
        if not power and not i.get("electric"):
            continue
        kind = "other" if power > 160 or i.get("electric") else "le160"
        band = year_band(int(i["year"]) if i.get("year") else None)
        if band:
            have[(kind, band)] = have.get((kind, band), 0) + 1
    return have


def run_wants(total: int, have: dict) -> dict:
    """Сколько машин каждой клетки (класс мощности, годы) взять за прогон, чтобы доли (75% до
    160 л.с., YEAR_BANDS) держались для каталога целиком: прошлый перекос выправляется,
    переполненные клетки в этот прогон не берём."""
    final = sum(have.values()) + total
    need = {(k, name): max(0, round(final * (SHARE_160 if k == "le160" else 1 - SHARE_160) * w) - have.get((k, name), 0))
            for k in KINDS for name, _, _, w in YEAR_BANDS}
    s = sum(need.values())
    if s > total:
        exact = {c: v * total / s for c, v in need.items()}
        # Округление с сохранением суммы: целые части, остаток — самым большим дробным
        need = {c: int(v) for c, v in exact.items()}
        for c in sorted(exact, key=lambda c: exact[c] - need[c], reverse=True)[:total - sum(need.values())]:
            need[c] += 1
    return need


class MixGuard:
    """Лимиты разнообразия MIX["limits"] = [на модель-год, на модель] — вместе с машинами сайта (have_names —
    {(марка, модель, год): машин}). Марка и модель — как у сайта (brand_model по названию)."""

    def __init__(self, have_names: dict):
        self.limits = MIX["limits"] or [10 ** 6, 10 ** 6]
        self.year, self.model = {}, {}
        for (make, model, year), n in have_names.items():
            self.year[(make, model, year)] = self.year.get((make, model, year), 0) + n
            self.model[(make, model)] = self.model.get((make, model), 0) + n

    @staticmethod
    def name(c):
        return c.get("_mm") or tuple(brand_model(c["name"], "", c.get("brandid"))[:2])

    def site_year(self, c) -> int:
        return self.year.get((*self.name(c), c.get("year")), 0)

    def allows(self, c) -> bool:
        return self.site_year(c) < self.limits[0] and self.model.get(self.name(c), 0) < self.limits[1]

    def take(self, c):
        k = (*self.name(c), c.get("year"))
        self.year[k] = self.year.get(k, 0) + 1
        self.model[self.name(c)] = self.model.get(self.name(c), 0) + 1


def pick_models(groups: dict, total: int, on_site: dict | None = None, have: dict | None = None,
                have_names: dict | None = None) -> list[dict]:
    """Не больше total машин: 75% до 160 л.с., 25% мощнее (электро, гибриды), по годам — YEAR_BANDS.
    Доли — для каталога целиком (have — состав сайта, см. run_wants).

    1) каждой модели, которой на сайте ещё нет, — по машине: класс и годы — из клетки
       «класс × годы», набранной меньше всего;
    2) добор по кругу по моделям, клетки вперемешку;
    3) каких-то лет не хватило — машины тех же классов любых лет.
    Модели, которых на сайте меньше (on_site), идут первыми, при равенстве — в случайном
    порядке: от прогона к прогону на сайт попадают разные модели.
    """
    on_site = on_site or {}
    # Новые машины — только со шкалой цены (4+ похожих объявлений che168): без неё на сайт не берём
    before = sum(map(len, groups.values()))
    groups = {k: [c for c in cars if c.get("price_stats")] for k, cars in groups.items()}
    groups = {k: cars for k, cars in groups.items() if cars}
    print(f"Новые машины со шкалой цены: {sum(map(len, groups.values()))} из {before}")
    # Сначала модели, которых на сайте меньше, среди них — самые массовые (больше объявлений в обходе)
    order = sorted(groups, key=lambda k: (on_site.get(k, 0), -len(groups[k]), random.random()))
    groups = {k: groups[k] for k in order}
    picked, used, count, taken = [], set(), {}, {}
    mix = MixGuard(have_names or {})
    rank = {name: i for i, (name, *_) in enumerate(YEAR_BANDS)}
    want = run_wants(total, have or {})
    print("Нужно за прогон: " + ", ".join(f"{'до 160' if k == 'le160' else 'мощнее'} {b} — {v}"
                                            for (k, b), v in want.items()))

    def fill_ratio(car):
        cell = (car["power"], year_band(car["year"]))
        return count.get(cell, 0) / want[cell] if want.get(cell) else 9

    def total_of(kind):
        return sum(v for (k, _), v in count.items() if k == kind)

    def take(c):
        mix.take(c)
        key = (series_key(c["name"]), c["power"])
        taken[key] = taken.get(key, 0) + 1
        picked.append(c)
        used.add(c["infoid"])
        k = (c["power"], year_band(c["year"]))
        count[k] = count.get(k, 0) + 1

    for cars in groups.values():
        # Сначала годы модели, которых на сайте нет (или меньше), среди них — массовые, потом по долям лет;
        # годы по очереди — по машине каждого года, потом по второй
        per_year = {}
        for c in cars:
            per_year[c["year"]] = per_year.get(c["year"], 0) + 1
        cars.sort(key=lambda c: (mix.site_year(c), -per_year.get(c["year"], 0), rank.get(year_band(c["year"]), 9)))
        nth, seq = {}, []
        for c in cars:
            nth[c["year"]] = nth.get(c["year"], 0) + 1
            seq.append((nth[c["year"]], len(seq), c))
        cars[:] = [c for *_, c in sorted(seq, key=lambda x: (x[0], x[1]))]
    new_models = [cars for k, cars in groups.items() if not on_site.get(k)]
    for cars in new_models:
        if len(picked) >= total:
            break
        fits = [c for c in cars if mix.allows(c)]
        best = min(fits, key=fill_ratio) if fits else None
        if best and fill_ratio(best) < 1:
            take(best)
    covered = len(picked)

    def candidates(kind, band):
        """Следующая машина класса kind и лет band — по кругу моделей, по машине с модели за круг."""
        pools = {k: iter([c for c in v if c["power"] == kind and (band is None or year_band(c["year"]) == band)])
                 for k, v in groups.items()}
        progress = True
        while progress:
            progress = False
            for key, it in pools.items():
                if taken.get((key, kind), 0) >= PER_MODEL_RUN[kind]:
                    continue   # разнообразие: не больше PER_MODEL_RUN машин модели за прогон
                c = next((c for c in it if c["infoid"] not in used and mix.allows(c)), None)
                if c:
                    progress = True
                    yield c

    open_cells = {cell: candidates(*cell) for cell, v in want.items() if v > 0}
    while open_cells and len(picked) < total:
        cell = min(open_cells, key=lambda c: count.get(c, 0) / want[c])
        c = next(open_cells[cell], None) if count.get(cell, 0) < want[cell] else None
        if c is None:
            del open_cells[cell]
            continue
        take(c)
    # Каких-то лет не хватило — добираем машинами тех же классов других лет, которых каталогу
    # ещё не хватает (переполненные годы не берём)
    for kind in KINDS:
        more = candidates(kind, None)
        while len(picked) < total and total_of(kind) < sum(v for (k, _), v in want.items() if k == kind):
            c = next(more, None)
            if c is None:
                break
            if want.get((kind, year_band(c["year"]))):
                take(c)
    share = total_of("le160") / len(picked) if picked else 0
    years = {name: sum(v for (_, b), v in count.items() if b == name) for name, *_ in YEAR_BANDS}
    print(f"Выбрано: новых моделей {covered} (на сайте нет {len(new_models)} из {len(groups)}), "
          f"машин {len(picked)} — до 160 л.с. {total_of('le160')} ({share:.0%}), "
          f"мощнее или электро/гибрид {total_of('other')}; по годам: " + ", ".join(f"{k} — {v}" for k, v in years.items()))
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
    resp = page.goto(url, wait_until="commit", timeout=30_000)
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
    # Мощность из объявления («纯电动 218马力», «2.0T 245马力») — точная для этой машины; по ней же
    # выбирается комплектация drom.ru (у Song Plus EV там версии 181 и 218 л.с.)
    listing_hp = int(hp.group(1)) if hp and 20 <= int(hp.group(1)) <= 2000 else None
    if listing_hp and fuel in ("Электро", "Последовательный гибрид (увеличенный запас хода)"):
        spec["Мощность, л.с."] = str(listing_hp)
    battery = re.search(r"(\d+(?:\.\d+)?)\s*kwh", rows.get("标准容量", ""), re.I)
    if battery:
        spec["Ёмкость батареи, кВт·ч"] = battery.group(1)
    rng = next((re.search(r"(\d{2,4})\s*km", v, re.I) for k, v in rows.items() if "纯电续航" in k), None)
    if rng:
        spec["Запас хода, км"] = rng.group(1)
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
        "hp": listing_hp,
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


# Модели, у которых атмосферный 2.0 обычно мощнее 160 л.с. (Toyota M20A — 171–178 л.с.)
_NA20_GT160 = re.compile(r"丰田|雷克萨斯|凯美瑞|亚洲龙|威兰达|荣放|RAV4|Lexus|Toyota", re.I)


# Марки, где почти все машины — электро или гибриды: без объёма в названии
# это не «мощность не оценить», а электромобиль/гибрид — группа «любой мощности»
NEV_MAKES = {"Tesla", "Xiaomi", "Nio", "Zeekr", "Xpeng", "Avatr", "Luxeed", "Li Auto", "AITO", "BYD",
             "Fangchengbao", "Denza", "Leapmotor", "Aion", "Neta", "Voyah", "IM", "Deepal", "Onvo", "Lynk & Co",
             "Chery Fengyun"}
_EV_MODELS = re.compile(r"Taycan|e-tron|\bEQ[A-Z]|\bi[X3457]\b|\biX\d|ID\.\s?\d|Lyriq|IQ", re.I)


def power_class(name: str, make: str | None = None) -> str | None:
    """«le160» — до 160 л.с., «other» — мощнее, None — оценить нечем (такие не берём).

    Мощности в списке che168 нет, поэтому оценка по двигателю, как для Кореи:
    атмосферный бензин до 2.0 л, турбо до 1.4 л, дизель и гибрид до 1.6 л.
    Электромобили и гибриды без объёма в названии — «other» (любой мощности);
    бензин и дизель без объёма в названии — None.
    """
    hp = re.search(r"(\d{2,4})\s*(?:PS|HP|马力)", name)   # «400PS», «280HP» — мощность прямо в названии
    if hp:
        return "le160" if int(hp.group(1)) <= 160 else "other"
    fuel = fuel_of(name, "")
    if fuel == "Электро" or fuel.startswith("Последовательный"):
        return "other"
    cc = guess_cc(name) or (2000 if re.search(r"\b(?:25|28)T\b", name) else None)   # Cadillac 25T/28T — 2.0T
    if not cc:
        nev = fuel == "Гибрид" or make in NEV_MAKES or _EV_MODELS.search(name) or re.search(r"新能源|\b\d{2}e\b", name)
        return "other" if nev else None
    trim = re.split(r"\d{4}\s*款", name, maxsplit=1)[-1]
    # Коды Audi/VW/BMW/Mercedes — всегда турбо; «1.5T», «2.0TD»
    turbo = bool(re.search(r"\d\.\dT|TFSI|TSI|TDI|涡轮|xDrive|sDrive|\d{2,3}Li?\b|\b(?:25|28)T\b", trim)) or bool(
        re.search(r"奔驰|Mercedes", name))
    if fuel == "Гибрид" and turbo:
        return "other"
    if turbo:
        return "le160" if cc <= 1400 else "other"
    if fuel in ("Гибрид", "Дизель"):
        return "le160" if cc <= 1600 else "other"
    if cc > 2000 or (cc > 1800 and _NA20_GT160.search(name)):
        return "other"
    return "le160"


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


# ---------- точная мощность ----------

class AutohomePower:
    """Мощность комплектации по её номеру в справочнике Autohome (specid из карточки che168):
    страница www.autohome.com.cn/spec/<specid>/ — «280kW 最大功率». Ответы запоминаются в
    autohome_cache.json (комплектация не меняется)."""

    def __init__(self, path):
        self.path = path
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Referer": "https://www.autohome.com.cn/",
                               "Accept-Language": "zh-CN,zh;q=0.9"})
        try:
            with open(path, encoding="utf-8") as f:
                self.cache = json.load(f)
        except (OSError, ValueError):
            self.cache = {}
        self.stats = {"cache": 0, "fetched": 0, "missing": 0}

    def info(self, specid) -> dict:
        """{"kw", "cc", "gearbox"} комплектации со страницы Autohome: блок «280kW 最大功率»,
        «3.0T 排量», «8挡手自一体 变速箱». Старые записи кэша (только kW) дочитываются."""
        if not specid or specid == "0":
            return {}
        cached = self.cache.get(specid)
        if isinstance(cached, dict):
            self.stats["cache"] += 1
            return cached
        time.sleep(random.uniform(1.0, 2.5))
        info = {}
        try:
            resp = self.s.get(f"https://www.autohome.com.cn/spec/{specid}/", timeout=30)
            if resp.status_code == 200:
                def stat(label):
                    m = re.search(rf">\s*([^<>]{{1,24}}?)\s*</div>\s*<div>\s*{label}", resp.text)
                    return m.group(1).strip() if m else ""
                kw = re.match(r"(\d{2,4}(?:\.\d)?)\s*kW", stat("最大功率"))
                if kw:
                    info["kw"] = float(kw.group(1))
                liters = re.match(r"(\d\.\d)\s*[TL]", stat("排量"))
                if liters:
                    info["cc"] = round(float(liters.group(1)) * 1000)
                    info["turbo"] = stat("排量").upper().endswith("T")
                box = stat("变速箱")
                if box and box not in ("暂无", "-"):
                    info["gearbox"] = box
        except Exception:
            pass
        if not info and isinstance(cached, (int, float)):
            info = {"kw": cached}
        self.stats["fetched" if info.get("kw") else "missing"] += 1
        if info.get("kw"):
            self.cache[specid] = info   # не нашли — не запоминаем, в следующий раз попробуем снова
        return info

    def kw(self, specid):
        return self.info(specid).get("kw")

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.cache, f, ensure_ascii=False, indent=0, sort_keys=True)



# ---------- оборудование комплектации (Autohome) ----------
# Номер опции на странице конфигурации Autohome → пункт блока «Комплектация» на сайте
# (сайт хранит пункты под корейскими названиями encar — так блок одинаковый для всех стран).
# Названия опций на странице частично спрятаны (символы в CSS), поэтому сверяем по номерам —
# они постоянные. Значение «●» — есть, «○» — за доплату (у конкретной машины неизвестно),
# «-» — нет; у составных опций — подпункты: 1 — есть, 2 — за доплату.
AH_PLAIN = {
    2: "브레이크 잠김 방지(ABS)", 10: "미끄럼 방지(TCS)", 11: "차체자세 제어장치(ESC)",
    13: "차선이탈 경보 시스템(LDWS)", 14: "긴급 제동 보조(AEB)", 29: "차선 유지 보조(LKA)",
    12: "후측방 경보 시스템", 31: "주차감지센서(전방, 후방)", 35: "주차 보조 시스템",
    83: "블랙박스", 107: "내비게이션", 115: "블루투스", 82: "헤드업 디스플레이(HUD)", 8411: "하이패스",
    57: "파워 전동 트렁크", 60: "루프랙", 63: "도난 방지 시스템", 64: "파워 도어록", 67: "스마트키",
    128: "오토 하이빔", 129: "오토 라이트", 137: "파워 윈도우", 73: "스티어링 휠 리모컨",
    74: "패들 시프트", 76: "열선 스티어링 휠", 89: "전동시트(운전석, 동승석)", 145: "커튼/블라인드(뒷좌석, 후방)",
    28: "에어백(운전석, 동승석)", 27: "에어백(사이드)", 26: "에어백(커튼)", 42: "전자제어 서스펜션(ECS)",
}
# Составные опции: номер → [(признак подпункта, пункт)]; признак — подстрока видимой части названия
# подпункта, None — любой подпункт. У подогрева сидений («加热») все символы обычно спрятаны —
# узнаём его по пустому названию.
_HEAT = lambda t: t == "" or "热" in t
AH_SUB = {
    21: [(None, "타이어 공기압센서(TPMS)")],
    32: [("倒车", "후방 카메라"), ("360", "후방 카메라"), ("全景", "후방 카메라"), ("360", "360도 어라운드 뷰"),
         ("全景", "360도 어라운드 뷰")],
    34: [(None, "크루즈 컨트롤(일반, 어댑티브)")],
    41: [(None, "전자제어 서스펜션(ECS)")],
    50: [(lambda t: "天窗" in t and "不可" not in t, "선루프"), ("全景", "파노라마 선루프")],
    66: [("遥控", "무선도어 잠금장치")],
    72: [("电动", "전동 조절 스티어링 휠")],
    86: [(None, "무선 충전")],
    92: [(None, "메모리 시트(운전석, 동승석)")],
    93: [(_HEAT, "열선시트(앞좌석)"), ("通", "통풍시트(운전석, 동승석)"), ("按摩", "마사지 시트")],
    94: [(_HEAT, "열선시트(뒷좌석)"), ("通", "통풍시트(뒷좌석)")],
    99: [(None, "전동시트(뒷좌석)")],
    116: [("CarPlay", "카플레이"), ("Android", "카플레이")],
    122: [("USB", "USB 단자"), ("Type-C", "USB 단자")],
    124: [("LED", "헤드램프(HID, LED)"), ("氙", "헤드램프(HID, LED)"), ("激光", "헤드램프(HID, LED)")],
    142: [("折叠", "전동접이 사이드 미러")],
    143: [("防眩", "ECM 룸미러")],
    149: [("雨量", "레인센서")],
    150: [("自动", "자동 에어컨")],
}
# Эти пункты показываем, только если они есть («нет» — неинтересно или не наверняка)
AH_ONLY_IF_ON = {"마사지 시트", "파노라마 선루프", "360도 어라운드 뷰", "전동시트(뒷좌석)", "통풍시트(뒷좌석)",
                 "커튼/블라인드(뒷좌석, 후방)", "메모리 시트(운전석, 동승석)"}
_SPAN = re.compile(r"<span[^>]*>\s*</span>")


def _ah_text(s) -> str:
    return _SPAN.sub("", str(s or "")).replace("&nbsp;", " ").strip()


def autohome_tokens(html: str) -> dict:
    """{specid: «номер:значение;…»} для всех комплектаций на странице конфигурации: значение «+», «-», «o»
    или «+подпункт|подпункт» (видимые символы подпунктов, которые есть)."""
    at = html.find("var option")
    start = html.find("{", at) if at >= 0 else -1
    if start < 0:
        return {}
    try:
        data, _ = json.JSONDecoder().raw_decode(html, start)
    except ValueError:
        return {}
    wanted = set(AH_PLAIN) | set(AH_SUB)
    out = {}
    for group in (data.get("result") or {}).get("configtypeitems") or []:
        for item in group.get("configitems") or []:
            if item.get("id") not in wanted:
                continue
            for v in item.get("valueitems") or []:
                spec = str(v.get("specid"))
                subs = v.get("sublist") or []
                if subs:
                    on = [_ah_text(x.get("subname")).replace("|", "/") for x in subs if x.get("subvalue") == 1]
                    token = ("+" + "|".join(on)) if on else "o"
                else:
                    value = _ah_text(v.get("value"))
                    token = "+" if "●" in value else "o" if "○" in value else "-" if value.startswith("-") else ""
                if token:
                    out.setdefault(spec, []).append(f"{item['id']}:{token}")
    return {k: ";".join(v) for k, v in out.items()}


def options_from_tokens(tokens: str) -> dict | None:
    """«номер:значение;…» → {"names": [есть], "known": [о чём известно]} для сайта."""
    have, known = set(), set()
    for part in (tokens or "").split(";"):
        num, _, token = part.partition(":")
        if not num.isdigit() or not token or token == "o":
            continue          # за доплату — у конкретной машины неизвестно
        num = int(num)
        on = token.startswith("+")
        subs = token[1:].split("|") if on and len(token) > 1 else []
        if num in AH_PLAIN:
            known.add(AH_PLAIN[num])
            if on:
                have.add(AH_PLAIN[num])
        for needle, key in AH_SUB.get(num, []):
            test = needle if callable(needle) else (lambda t, n=needle: n is None or n in t)
            hit = on and any(test(t) for t in (subs or [""]))
            if hit:
                have.add(key)
            # «Нет» — только если нет всей опции (или любой подпункт годится); у подпунктов часть
            # символов спрятана — отсутствие нужного названия ещё не значит, что его нет.
            # Исключение — сиденья: подогрев спрятан целиком, остальное видно
            if hit or token == "-" or needle is None or num in (93, 94):
                known.add(key)
    known -= {k for k in AH_ONLY_IF_ON if k not in have}
    if not have:
        return None
    return {"names": sorted(have), "known": sorted(known | have)}


class AutohomeOptions:
    """Оборудование комплектации: страница car.autohome.com.cn/config/spec/<specid>.html — на ней
    вся линейка модели, запоминаем все комплектации (autohome_options.json), чтобы следующие
    машины той же модели не открывать. limit — сколько страниц открыть за прогон."""

    def __init__(self, path, limit=800):
        self.path, self.limit = path, limit
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Referer": "https://car.autohome.com.cn/",
                               "Accept-Language": "zh-CN,zh;q=0.9"})
        try:
            with open(path, encoding="utf-8") as f:
                self.cache = json.load(f)
        except (OSError, ValueError):
            self.cache = {}
        self.stats = {"cache": 0, "fetched": 0, "missing": 0}
        self.failed = set()

    def get(self, specid) -> dict | None:
        specid = str(specid or "")
        if not specid or specid == "0" or specid in self.failed:
            return None
        if specid in self.cache:
            self.stats["cache"] += 1
            return options_from_tokens(self.cache[specid])
        if self.stats["fetched"] + self.stats["missing"] >= self.limit:
            return None
        time.sleep(random.uniform(1.0, 2.5))
        tokens = {}
        try:
            resp = self.s.get(f"https://car.autohome.com.cn/config/spec/{specid}.html", timeout=30)
            if resp.status_code == 200:
                tokens = autohome_tokens(resp.text)
        except Exception:
            pass
        if specid not in tokens:
            self.stats["missing"] += 1
            self.failed.add(specid)
            return None
        self.stats["fetched"] += 1
        self.cache.update(tokens)
        return options_from_tokens(tokens[specid])

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.cache, f, ensure_ascii=False, indent=0, sort_keys=True)


def drom_cc(car: dict, make, model, drom, hp) -> int | None:
    """Объём с drom.ru для сверки: комплектация машины без фильтра по объёму — если она одна
    и её мощность совпадает с известной (hp), её объём. Autohome у части комплектаций пишет
    не тот объём: «Q3 2024款 35 TFSI» — «1.4T» при 118 кВт, а 160 л.с. — это уже 1.5."""
    if not (drom and make and model and hp):
        return None
    found = drom.power(drom_car(car, make, model, guess=False))
    if found and found.get("liters") and abs(found["hp"] - hp) <= 2:
        return round(found["liters"] * 1000)
    return None


def cc_fix(car: dict, items: dict, autohome, drom=None) -> dict:
    """Машине с сайта — точный объём, если на сайте другой: раньше при закрытом объявлении
    объём угадывался по названию («35 TFSI» → 1.4 вместо 1.5). Источник — Autohome по номеру
    комплектации, а если там нет — drom.ru (только из кэша, без новых запросов).
    → {"cc"} / {"cc", "hp", "tech"} для отметки «ещё в продаже» или {}."""
    item = items.get(str(car["infoid"])) or {}
    if item.get("electric"):
        return {}
    try:
        have = round(float(item.get("cc") or 0))
    except ValueError:
        have = 0
    if not have:
        return {}
    info = autohome.cache.get(str(car.get("specid") or "")) if autohome else None
    if isinstance(info, dict) and info.get("cc"):
        cc = info["cc"]
        if drom and info.get("kw"):
            make, model = brand_model(car["name"], "", car.get("brandid"))[:2]
            with drom.cached_only():
                cc = drom_cc(car, make, model, drom, round(info["kw"] * 1.35962)) or cc
        return {"cc": cc} if have != cc else {}
    # drom.ru — только у машин из списка (объём по названию, мощность по нему же с drom.ru);
    # объём со страницы объявления точный, его не трогаем
    if not (drom and item.get("detail_spec") is False and item.get("hp")):
        return {}
    make, model = brand_model(car["name"], "", car.get("brandid"))[:2]
    if not (make and model):
        return {}
    with drom.cached_only():
        found = drom.power(drom_car(car, make, model, guess=False))
        if not found or not found.get("liters") or round(found["liters"] * 1000) == have:
            return {}
        tech = drom.tech(found.get("trim"))
    out = {"cc": round(found["liters"] * 1000)}
    if str(found["hp"]) != str(item["hp"]):
        out["hp"] = found["hp"]
    return {**out, **({"tech": tech} if tech else {})}


EV_FIX_LIMIT = int(os.environ.get("CHE168_EV_FIX") or "80")
_ev_fixed = [0]


def ev_fix(car: dict, items: dict, drom) -> dict:
    """Электромобилю (или последовательному гибриду) с сайта — исправленный тип топлива и
    характеристики drom.ru с 30-минутной мощностью (по ней утильсбор): раньше «宋PLUS EV 605KM»
    без «纯电» в названии записывался бензиновым с объёмом 1500. Не больше EV_FIX_LIMIT за прогон.
    → {"fuel", "tech", "hp"} для отметки «ещё в продаже» или {}."""
    item = items.get(str(car["infoid"])) or {}
    fuel = fuel_of(car["name"], "")
    ev = fuel in ("Электро", "Последовательный гибрид (увеличенный запас хода)")
    # Гибрид без 30-минутной мощности (сайт показывает цену «от») — тоже дошлём характеристики drom.ru
    if not (ev or item.get("needs30")) or not item or not drom:
        return {}
    wrong_fuel = fuel == "Электро" and not item.get("electric")
    if not wrong_fuel and not item.get("needs30") and item.get("power30"):
        return {}
    if _ev_fixed[0] >= EV_FIX_LIMIT:
        return {}
    make, model = brand_model(car["name"], "", car.get("brandid"))[:2]
    out = {"fuel": fuel} if wrong_fuel else {}
    if make and model:
        _ev_fixed[0] += 1
        try:
            car = {**car, "hp_listing": int(re.sub(r"\D", "", str(item.get("hp") or "")) or 0) or None}
        except ValueError:
            pass
        found = drom.power(drom_car(car, make, model, guess=False))
        tech = drom.tech(found.get("trim")) if found else None
        if tech:
            out["tech"] = tech
        if found and str(found["hp"]) != str(item.get("hp") or ""):
            out["hp"] = found["hp"]
    return out


def drom_car(car: dict, make: str, model: str, cc=None, guess=True) -> dict:
    """Машина che168 → признаки для поиска комплектации на drom.ru.
    cc — точный объём (объявление, Autohome); без него — догадка по названию (guess=False — без объёма)."""
    import drom_specs
    name = car["name"]
    model_year = re.search(r"(20\d{2})\s*款", name)
    fuel = fuel_of(name, "")
    trim = " ".join(x for x in (latin_trim(name), car.get("battery_kwh") and f"{car['battery_kwh']:g} kWh") if x) or None
    return {
        "make": make, "model": model, "market": "china",
        "year": int(model_year.group(1)) if model_year else car["year"],
        "cc": (cc or (guess_cc(name) if guess else None)) if fuel != "Электро" else None,
        "fuel": drom_specs.norm_fuel(fuel), "drive": drom_specs.norm_drive(name),
        "trans": drom_specs.norm_trans(name), "trim": trim,
        # Мощность из объявления — подсказка для выбора комплектации drom.ru
        "hp": car.get("hp_listing"),
    }


def drom_tech(car: dict, make, model, drom, counts, found=None, cc=None):
    """Технические характеристики комплектации с drom.ru (разгон, расход, размеры…) или None."""
    if not (drom and make and model):
        return None
    found = found or drom.power(drom_car(car, make, model, cc))
    tech = drom.tech(found.get("trim")) if found else None
    if tech:
        counts["tech"] = counts.get("tech", 0) + 1
    return tech


def add_power(spec: dict, car: dict, make, model, autohome, drom, counts, cc_exact=True):
    """Точная мощность в характеристики: Autohome по номеру комплектации, иначе drom.ru.
    cc_exact=False — объём в spec угадан по названию: тогда он с Autohome, а нет там — с drom.ru.
    → технические характеристики комплектации с drom.ru (или None)."""
    cc = lambda: int(spec.get("Рабочий объём цилиндров (см³)") or 0) or None
    if spec.get("Максимальная мощность (кВт)") or spec.get("Мощность, л.с."):
        return drom_tech(car, make, model, drom, counts, cc=cc())
    info = autohome.info(car.get("specid")) if autohome else {}
    # Объём и коробка с Autohome — у машин «из списка» (объявление закрыто капчей) их иначе нет,
    # а без объёма не посчитать таможню. Объём комплектации Autohome точнее догадки по названию:
    # «Q3 2024款 35 TFSI» — это уже 1.5T, а не 1.4T, как у прежних 35 TFSI
    if info.get("cc") and spec.get("Тип топлива") != "Электро":
        spec["Рабочий объём цилиндров (см³)"] = str(info["cc"])
        cc_exact = True
    if info.get("gearbox") and not spec.get("Коробка передач"):
        box = info["gearbox"]
        spec["Коробка передач"] = next((ru for zh, ru in GEARBOX.items() if zh in box), None) or box
    kw = info.get("kw")
    if kw:
        spec["Максимальная мощность (кВт)"] = f"{kw:g}"
        counts["autohome"] += 1
        if spec.get("Тип топлива") != "Электро":
            checked = drom_cc(car, make, model, drom, round(kw * 1.35962))
            if checked and checked != cc():
                spec["Рабочий объём цилиндров (см³)"] = str(checked)
                counts["cc_drom"] = counts.get("cc_drom", 0) + 1
        return drom_tech(car, make, model, drom, counts, cc=cc())
    found = None
    if drom and make and model and not cc_exact and spec.get("Тип топлива") != "Электро":
        # Объём угадан — комплектацию на drom.ru ищем без него, и объём берём оттуда
        found = drom.power(drom_car(car, make, model, guess=False))
        if found and found.get("liters"):
            spec["Рабочий объём цилиндров (см³)"] = str(round(found["liters"] * 1000))
            counts["cc_drom"] = counts.get("cc_drom", 0) + 1
    if not found and drom and make and model:
        found = drom.power(drom_car(car, make, model, cc()))
    if found:
        spec["Мощность, л.с."] = str(found["hp"])
        if found.get("hp_total"):
            spec["Суммарная мощность гибрида, л.с."] = str(found["hp_total"])
        counts["drom"] += 1
        return drom_tech(car, make, model, drom, counts, found)
    counts["none"] += 1
    return None


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


def prune_no_price() -> None:
    """Удалить с сайта машины без шкалы цены — поиск цен им закончен и больше не находит."""
    if not BN_AUTO_URL or not BN_AUTO_IMPORT_TOKEN:
        return
    try:
        resp = requests.post(f"{BN_AUTO_URL}/api/live-listings/prune-no-price", json={"source": "che168"},
                             headers={"Authorization": f"Bearer {BN_AUTO_IMPORT_TOKEN}"}, timeout=120)
        print(f"Удаление машин без шкалы цены: {resp.status_code} {resp.text[:200]}")
    except Exception as error:
        print(f"Удаление машин без шкалы цены не удалось: {error}")


def fetch_known() -> dict:
    """Все объявления che168 на сайте: id → марка, модель, complete (фото, цена, данные для расчёта)…"""
    if not BN_AUTO_URL or not BN_AUTO_IMPORT_TOKEN:
        return {}
    try:
        resp = requests.get(f"{BN_AUTO_URL}/api/live-listings/known", params={"source": "che168", "all": "1"},
                            headers={"Authorization": f"Bearer {BN_AUTO_IMPORT_TOKEN}"}, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        MIX["limits"] = data.get("mix")
        SITE_ITEMS.update({str(i["id"]): i for i in data.get("items") or []})
        return dict(SITE_ITEMS)
    except Exception as error:
        print(f"Список известных объявлений не получен ({error}) — разбираем всё")
        return {}


TRIED_PATH = os.path.join(ROOT, "che168_tried.json")
# Машину, которую не удалось заполнить (нет объёма или мощности — страница объявления не открылась), снова не
# выбираем столько дней: иначе каждый прогон тратил отбор на те же ~50 машин и снова их отбрасывал
TRIED_DAYS = int(os.environ.get("CHE168_TRIED_DAYS") or "7")
# Пауз подряд, если che168 не отдаёт объявления (3–5 мин, дальше дольше); не помогли — прокси, потом данные списка
DETAIL_COOLS = int(os.environ.get("CHE168_DETAIL_COOLS") or "3")
# Пауза между объявлениями, с: che168 ставит капчу, если открывать их часто (3 подряд за 25 с — уже капча).
# В задаче объявлений (CHE168_PHASE=detail) — темп человека, по одному раз в 25–45 с
DETAIL_PAUSE = tuple(float(x) for x in (os.environ.get("CHE168_DETAIL_PAUSE") or "2,4.5").split(","))
# Прогон в две задачи GitHub: «scan» обходит списки (сотни страниц — после них che168 ставит капчу на объявления)
# и сохраняет отобранные машины в PICKED_PATH; «detail» — на новой машине GitHub, с новым IP — открывает их
# объявления. Пусто — всё в одной задаче, как раньше
PHASE = (os.environ.get("CHE168_PHASE") or "").strip()
PICKED_PATH = os.path.join(ROOT, "che168_picked.json")


def save_picked(cars: list[dict]) -> None:
    with open(PICKED_PATH, "w", encoding="utf-8") as f:
        json.dump({"cars": cars, "brand_ids": BRAND_IDS}, f, ensure_ascii=False)
    print(f"Отобранные машины ({len(cars)}) сохранены — объявления откроет следующая задача с другим IP")


def load_picked() -> list[dict]:
    try:
        with open(PICKED_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        print("Отобранных машин нет (обход ничего не выбрал или не сохранил)")
        return []
    BRAND_IDS.update(data.get("brand_ids") or {})
    return data.get("cars") or []


def load_tried() -> dict:
    try:
        with open(TRIED_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    today = date.today().toordinal()
    return {k: d for k, d in data.items() if today - d <= TRIED_DAYS}


def save_tried(tried: dict) -> None:
    with open(TRIED_PATH, "w", encoding="utf-8") as f:
        json.dump(tried, f, ensure_ascii=False, sort_keys=True)


def good_known(items: dict) -> set:
    """Машины с сайта, которые заново не открываем: полная информация и крупное фото.
    Остальные (без фото, модели, цены, двигателя или с миниатюрой) — загрузим заново."""
    ids = {k for k, i in items.items() if i.get("complete", True) and (i.get("photo_kb") or 999) >= 25}
    print(f"На сайте: {len(items)}, с полной информацией {len(ids)} — их объявления не открываем; "
          f"неполные ({len(items) - len(ids)}) загрузим заново, если встретятся")
    return ids


def run_size(items: dict) -> int:
    """Сколько новых машин добавить: вручную (CHE168_TOTAL); до заполнения каталога (FILL_TARGET) —
    по FILL_PER_RUN за прогон; потом в дни обновления (первый прогон дня) — UPDATE_NEW порциями
    по UPDATE_BATCH с паузой UPDATE_PAUSE; 0 — сейчас ничего не нужно."""
    global BATCH, BATCH_PAUSE
    if TOTAL:
        return TOTAL
    # До 5500 считаем машины со шкалой цены: без неё машина каталог не заполняет
    good = sum(1 for i in items.values() if i.get("complete") and i.get("published") and i.get("has_gauge"))
    # Сначала шкала цены у каждой машины сайта: пока без неё GAUGE_FIRST_MAX+ машин — новых не добавляем,
    # прогон обходит модели этих машин и считает им шкалу
    if GAUGE_FIRST and lacking_gauge(items) > GAUGE_FIRST_MAX:
        print(f"Без шкалы цены {lacking_gauge(items)} машин сайта — новых не добавляем, ищем им цены")
        return 0
    if good < FILL_TARGET:
        n = min(FILL_PER_RUN, FILL_TARGET - good)
        print(f"Заполнение каталога: на сайте {good} из {FILL_TARGET} — добавим {n}")
        # Каталог ещё не заполнен — workflow сразу запустит следующий прогон (без остановки)
        open(os.path.join(ROOT, "continue_fill"), "w").close()
        return n
    now = time.gmtime()
    if now.tm_wday in UPDATE_DAYS and now.tm_hour < 8:
        BATCH, BATCH_PAUSE = UPDATE_BATCH, UPDATE_PAUSE
        print(f"Каталог заполнен ({good}) — обновление: до {UPDATE_NEW} новых, порции по {BATCH} с паузой {BATCH_PAUSE:g} мин")
        return UPDATE_NEW
    print(f"Каталог заполнен ({good}), сейчас не время обновления — прогон окончен")
    return 0


def missing(x: dict) -> str | None:
    """Чего не хватает объявлению для сайта (None — информация полная): фото, цена, год, марка,
    модель и то, по чему считается таможня — объём (кроме электромобилей) и точная мощность: без неё
    сайт машину не сохраняет («нет фото или точной мощности»)."""
    spec = x.get("spec") or {}
    ev = spec.get("Тип топлива") == "Электро"
    power = spec.get("Максимальная мощность (кВт)") or spec.get("Мощность, л.с.") \
        or re.search(r"\d{2,4}\s*л\.с\.", spec.get("Двигатель") or "")
    for field, ok in (("фото", x.get("photo_url")), ("цена", x.get("price_value")), ("год", x.get("year")),
                      ("марка", x.get("make")), ("модель", x.get("model")),
                      ("объём", ev or spec.get("Рабочий объём цилиндров (см³)")), ("мощность", power)):
        if not ok:
            return field
    return None


def complete(x: dict) -> bool:
    return missing(x) is None


def push(listings):
    from collections import Counter
    reasons = Counter(missing(x) for x in listings if "spec" in x and missing(x))
    listings = [x for x in listings if "spec" not in x or complete(x)]
    if reasons:
        print("Не отправлены (неполные): " + ", ".join(f"нет {k} — {v}" for k, v in reasons.most_common()))
    if not listings:
        return
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


def gather(page, known: set, total: int, items: dict, touched: list) -> list[dict]:
    """Все модели (обход марок и моделей), а если не вышло — общий список, как раньше."""
    if ALL_MODELS:
        groups = scan_models(page, known, touched)
        if groups is False:
            return []
        if groups:
            # Сколько машин каждой модели уже на сайте — по марке и модели
            by_name = {}
            for i in items.values():
                if i.get("complete") and i.get("published"):
                    by_name[(i.get("make"), i.get("model"))] = by_name.get((i.get("make"), i.get("model")), 0) + 1
            on_site = {}
            for key, cars in groups.items():
                make, model = brand_model(cars[0]["name"], "", cars[0].get("brandid"))[:2]
                on_site[key] = by_name.get((make, model), 0)
            have = catalog_have(items.values())
            have_names = {}
            for i in items.values():
                if i.get("complete") and i.get("published") and i.get("make"):
                    k = (i["make"], i.get("model"), int(i["year"]) if str(i.get("year") or "").isdigit() else None)
                    have_names[k] = have_names.get(k, 0) + 1
            print("На сайте по годам: " + ", ".join(f"{name} — {sum(v for (_, b), v in have.items() if b == name)}"
                                                    for name, *_ in YEAR_BANDS)
                  + f"; до 160 л.с. {sum(v for (k, _), v in have.items() if k == 'le160')}, "
                    f"мощнее {sum(v for (k, _), v in have.items() if k == 'other')}")
            return pick_models(groups, total, on_site, have, have_names)
    return collect(page, known, total)


def main():
    started = time.time()
    items = fetch_known()
    known = good_known(items)
    tried = load_tried()
    skip = {k for k in tried if k not in known}
    if skip:
        print(f"Не удалось заполнить в последние {TRIED_DAYS} дн. — не выбираем снова: {len(skip)} машин")
    known |= skip
    total = run_size(items)
    if not total and not stats_due(items):
        return
    touched = []
    session = http_session()
    listings = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        # Обход всех моделей — сотни страниц списка: их che168 отдаёт и напрямую, и так
        # быстрее; китайский прокси — для объявлений (переключаемся на капче, см. ниже)
        list_proxy = USE_PROXY and not ALL_MODELS and PHASE != "detail"
        on_proxy = list_proxy
        context = new_context(browser, list_proxy)
        page = context.new_page()
        if PHASE == "detail":
            # Списки обошла прошлая задача — здесь только объявления отобранных машин (кроме уже добавленных)
            cars = [c for c in load_picked() if c["infoid"] not in known]
        else:
            cars = gather(page, known, total, items, touched)
            if not cars and total and list_proxy:
                # Прокси не отвечает (бесплатные быстро умирают) — список пробуем напрямую
                print("Через прокси список не получен — пробуем напрямую")
                context.close()
                on_proxy = False
                context = new_context(browser, False)
                page = context.new_page()
                cars = gather(page, known, total, items, touched)
            elif not cars and total and PROXY_SERVER:
                # Напрямую che168 не отдал список — пробуем через прокси
                print("Напрямую список не получен — пробуем через прокси")
                context.close()
                on_proxy = True
                context = new_context(browser, True)
                page = context.new_page()
                cars = gather(page, known, total, items, touched)
        print(f"Отобрано новых: {len(cars)}, машин с сайта встречено в обходе: {len(touched)}, марок по номерам: {len(BRAND_IDS)}")
        # Точная мощность: Autohome по номеру комплектации, запасной путь — каталог drom.ru
        # (его таблицы дорисовываются скриптом — открываем в браузере, без прокси)
        import drom_specs
        autohome = AutohomePower(os.path.join(ROOT, "autohome_cache.json"))
        # Оборудование для блока «Комплектация» — по номеру комплектации с Autohome
        equipment = AutohomeOptions(os.path.join(ROOT, "autohome_options.json"),
                                    limit=int(os.environ.get("AUTOHOME_OPTION_PAGES") or "600"))

        def no_options(infoid):
            """Машина на сайте без блока «Комплектация» (добавлена раньше) — дошлём оборудование."""
            return items.get(str(infoid), {}).get("has_options") is False
        drom_context = browser.new_context(user_agent=UA, locale="ru-RU", timezone_id="Asia/Vladivostok")
        drom = drom_specs.DromCatalog(os.path.join(ROOT, "drom_cache.json"), drom_specs.playwright_fetcher(drom_context),
                                      max_requests=int(os.environ.get("DROM_MAX_PAGES") or "300"))
        power_counts = {"autohome": 0, "drom": 0, "none": 0}
        # Отметка «ещё в продаже» машинам с сайта, встреченным в обходе (и исправленный объём)
        seen_cars = [{"external_id": c["infoid"], "price_value": c["price_cny"], "mileage_km": c["mileage_km"],
                      "source_url": f"https://www.che168.com/dealer/{c['dealerid']}/{c['infoid']}.html",
                      **cc_fix(c, items, autohome, drom), **ev_fix(c, items, drom),
                      **({"price_stats": c["price_stats"], "stats_key": c.get("stats_key")} if c.get("price_stats") else {})}
                     for c in {c["infoid"]: c for c in touched}.values()]
        print(f"Исправлен объём у машин с сайта: {sum('cc' in x for x in seen_cars)} "
              f"(и мощность у {sum('hp' in x for x in seen_cars)}); электромобили: тип топлива у "
              f"{sum('fuel' in x for x in seen_cars)}, характеристики drom.ru у {sum('tech' in x for x in seen_cars)}")
        push(seen_cars)
        # Шкала цены машинам сайта, не встреченным в обходе: нет её или старше CHE168_STATS_DAYS дней
        if PHASE != "detail":
            push(stale_stats(items, {str(c["infoid"]) for c in touched} | {str(c["infoid"]) for c in cars}))
        if PHASE == "scan":
            save_picked(cars)
            autohome.save()
            drom.save()
            browser.close()
            finish_scan(items, touched, total)
            return

        done = failed = degraded = degraded_saved = from_list = 0
        list_only = False   # объявления закрыты капчей — дальше только данные списка
        fails_in_row = 0
        detail_cools = 0    # пауз подряд из-за блока объявлений (после DETAIL_COOLS — прокси, потом список)
        opened_ids = set()  # объявления, которые правда открылись (их неполноту запоминаем надолго)
        # Обход закончился паузой che168 — объявления открываем, когда она пройдёт
        if GATE.until > time.time():
            left = GATE.until - time.time()
            print(f"che168 ещё ограничивает запросы — ждём {left / 60:.1f} мин перед объявлениями")
            time.sleep(left + random.uniform(20, 60))

        def cool_down(why) -> bool:
            """Блок объявлений: пауза 3–5 мин (дальше дольше) и ещё попытка. False — паузы не помогли."""
            nonlocal detail_cools
            if detail_cools >= DETAIL_COOLS:
                return False
            pause = min(12.0, random.uniform(3, 5) * (1.5 ** detail_cools))
            detail_cools += 1
            print(f"che168 не отдаёт объявления ({why}) — пауза {pause:.1f} мин и ещё попытка ({detail_cools}/{DETAIL_COOLS})")
            time.sleep(pause * 60)
            return True
        breaks = random.randint(30, 45)

        def add(car, url, body=None, d=None):
            nonlocal done, from_list
            if d is None:
                d = {"spec": spec_from_name(car), "photos": []}
                from_list += 1
            make, model = brand_model(car["name"], body or "", car.get("brandid"))
            try:
                car["hp_listing"] = d.get("hp")
                car["battery_kwh"] = float(d["spec"].get("Ёмкость батареи, кВт·ч") or 0) or None
            except ValueError:
                pass
            tech = add_power(d["spec"], car, make, model, autohome, drom, power_counts, cc_exact=body is not None)
            opts = equipment.get(car.get("specid"))
            listings.append({
                "external_id": car["infoid"], "make": make, "model": model, "title": car["name"],
                "year": car["year"], "mileage_km": car["mileage_km"], "price_value": car["price_cny"],
                "photo_url": fetch_photo(session, d["photos"] + photo_candidates(car.get("image"))),
                "spec": d["spec"] or None, "source_url": url, **({"options": opts} if opts else {}),
                **({"tech": tech} if tech else {}),
                **({"price_stats": car["price_stats"], "stats_key": car.get("stats_key")} if car.get("price_stats") else {}),
            })
            done += 1
            src = "из списка" if body is None else "из объявления"
            print(f"[{done}] {make or '?'} {model or ''} {car['year']} — {car['price_cny']} ¥, характеристик: {len(d['spec'])} ({src})")

        # Порциями: BATCH машин — отправка на сайт — пауза BATCH_PAUSE минут. Машины появляются
        # на сайте по ходу прогона, а если прогон оборвётся — отправленное останется.
        pushed = 0
        for idx, car in enumerate(cars):
            if time.time() - started > RUN_MINUTES * 60:
                print(f"Прошло {RUN_MINUTES:g} мин — остальные {len(cars) - idx} машин в следующий прогон")
                break
            if idx and idx % BATCH == 0:
                push(listings[pushed:])
                pushed = len(listings)
                pause = BATCH_PAUSE * random.uniform(0.7, 1.3)
                print(f"=== Отправлено {idx} из {len(cars)} — пауза {pause:.1f} мин ===")
                time.sleep(pause * 60)
                list_only = False   # после паузы ещё раз пробуем открыть объявления
                fails_in_row = 0
                detail_cools = 0
            url = f"https://www.che168.com/dealer/{car['dealerid']}/{car['infoid']}.html"
            if car["infoid"] in known:
                opts = equipment.get(car.get("specid")) if no_options(car["infoid"]) else None
                listings.append({"external_id": car["infoid"], "price_value": car["price_cny"],
                                 "mileage_km": car["mileage_km"], "source_url": url, **({"options": opts} if opts else {}),
                                 **cc_fix(car, items, autohome, drom), **ev_fix(car, items, drom),
                                 **({"price_stats": car["price_stats"], "stats_key": car.get("stats_key")} if car.get("price_stats") else {})})
                continue
            if list_only:
                add(car, url)
                continue
            try:
                body = None
                while body is None:
                    try:
                        body = fetch_detail(page, car)
                    except Blocked as reason:
                        if not cool_down(reason):
                            raise
                    except Degraded:
                        raise
                    except Exception as error:
                        # Одна машина не открылась — её по данным списка, следующую пробуем; вторая подряд —
                        # пауза и ещё попытка; паузы не помогли — дальше только данные списка
                        fails_in_row += 1
                        if fails_in_row >= 2 and cool_down(str(error).splitlines()[0][:60]):
                            fails_in_row = 0
                            continue
                        raise
            except Degraded as first:
                try:
                    if degraded_saved < 3:
                        save_debug(f"detail_degraded_{car['infoid']}.html", page.content())
                        degraded_saved += 1
                    print(f"[{car['infoid']}] страница без характеристик ({first}) — пауза и ещё попытка")
                    human_pause(10, 20)
                    body = fetch_detail(page, car)
                except Exception:
                    body = None
                if body is None:
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
                if fails_in_row >= 2:
                    # che168 перестал отдавать объявления (подвисает) — не ждём на каждой машине
                    print("Объявления не открываются второй раз подряд — дальше только данные списка")
                    list_only = True
                add(car, url)
                continue
            fails_in_row = 0
            detail_cools = 0
            opened_ids.add(str(car["infoid"]))
            if degraded_saved < 5 and done < 2:
                save_debug(f"detail_{car['infoid']}.html", body)
            add(car, url, body, parse_detail(body, car))
            human_pause(*DETAIL_PAUSE)
            if done >= breaks:
                # Темп человека: перерыв 40–90 с каждые 30–45 машин (раньше 1–2,5 мин каждые 25–40)
                human_pause(40, 90)
                breaks = done + random.randint(30, 45)
        # Машинам с сайта, встреченным в обходе, без блока «Комплектация» — оборудование
        sent_ids = {x["external_id"] for x in listings}
        backfill = 0
        for c in {c["infoid"]: c for c in touched}.values():
            if c["infoid"] in sent_ids or not no_options(c["infoid"]):
                continue
            if time.time() - started > (RUN_MINUTES + 12) * 60:
                break     # не упереться в лимит GitHub (355 мин): остальным — в следующий прогон
            opts = equipment.get(c.get("specid"))
            if opts:
                listings.append({"external_id": c["infoid"], "price_value": c["price_cny"], "mileage_km": c["mileage_km"],
                                 "source_url": f"https://www.che168.com/dealer/{c['dealerid']}/{c['infoid']}.html",
                                 "options": opts})
                backfill += 1
        print(f"Оборудование (Autohome): {equipment.stats}, дослано машинам с сайта {backfill}")
        autohome.save()
        equipment.save()
        drom.save()
        print(f"Мощность: из Autohome {power_counts['autohome']}, из drom.ru {power_counts['drom']} "
              f"(совпадений drom.ru: {drom.stats}, страниц drom.ru {drom.requests}), не найдена {power_counts['none']}; "
              f"технические характеристики с drom.ru у {power_counts.get('tech', 0)}, объём с drom.ru у {power_counts.get('cc_drom', 0)}; "
              f"Autohome: {autohome.stats}")
        browser.close()

    today = date.today().toordinal()
    for x in listings:
        if "spec" in x and not complete(x):
            # Объявление открылось, а данных нет — не выбираем TRIED_DAYS дней; не открылось (блок) — 2 дня
            xid = str(x["external_id"])
            tried[xid] = today if xid in opened_ids else today - max(0, TRIED_DAYS - 2)
    save_tried(tried)
    print(f"Итого: новых {sum('spec' in x for x in listings)} (из них только по списку {from_list}), "
          f"уже на сайте {sum('spec' not in x for x in listings)}, урезанных страниц {degraded}, ошибок {failed}")
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump([{k: v for k, v in x.items() if k != "photo_url"} for x in listings], f, ensure_ascii=False, indent=2)
    push(listings[pushed:])
    if PHASE != "detail":
        finish_scan(items, touched, total)


def finish_scan(items: dict, touched: list, total: int) -> None:
    """После обхода списков: удаление машин без шкалы цены и решение о следующем прогоне."""
    # Цены машинам сайта без шкалы искали по всем их маркам и моделям — кому не нашлась, удаляем
    pub = sum(1 for i in items.values() if i.get("published"))
    if GAUGE_SEARCH["done"] and len(touched) >= max(50, pub // 10):
        prune_no_price()
    else:
        print("Поиск цен машинам без шкалы прерван (проверка che168 или время) — без удаления, продолжим в следующем прогоне")
    # Ищем цены машинам сайта (новых не добавляем) — следующий прогон сразу, пока машин без шкалы заметно меньше
    if not total and GAUGE_FIRST:
        after = fetch_known()
        was, now = lacking_gauge(items), lacking_gauge(after) if after else None
        print(f"Без шкалы цены: было {was}, стало {now}")
        if now is not None and now > GAUGE_FIRST_MAX and was - now >= 20:
            open(os.path.join(ROOT, "continue_fill"), "w").close()


if __name__ == "__main__":
    main()
