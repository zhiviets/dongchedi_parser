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
import math
import os
import random
import re
import time

import requests
from playwright.sync_api import sync_playwright

from china_brand_map import extract_brand_model
from weekly_scraper import compress_photo_to_data_url

# Сколько новых машин за прогон (0 — само: до FILL_TARGET на сайте, потом раз в неделю)
TOTAL = int(os.environ.get("CHE168_TOTAL") or "0")
# Заполнение каталога: пока машин с полной информацией на сайте меньше FILL_TARGET —
# каждый прогон добавляет до FILL_PER_RUN новых; дальше раз в неделю (WEEKLY_DAY,
# 0 — понедельник) — WEEKLY_NEW, в остальные дни прогон сразу заканчивается
FILL_TARGET = int(os.environ.get("CHE168_FILL_TARGET") or "5500")
FILL_PER_RUN = int(os.environ.get("CHE168_FILL_PER_RUN") or "800")
# Через сколько минут от начала прогона перестать открывать объявления и отправить собранное
# (предел GitHub Actions — 6 часов)
RUN_MINUTES = float(os.environ.get("CHE168_RUN_MINUTES") or "320")
WEEKLY_NEW = int(os.environ.get("CHE168_WEEKLY_NEW") or "1000")
WEEKLY_DAY = int(os.environ.get("CHE168_WEEKLY_DAY") or "3")
# Доля машин до 160 л.с. (проходных по утильсбору), остальные — любой мощности
SHARE_160 = float(os.environ.get("CHE168_SHARE_160") or "0.75")
MIN_YEAR = int(os.environ.get("CHE168_MIN_YEAR") or "2017")
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
BATCH_PAUSE = float(os.environ.get("CHE168_BATCH_PAUSE") or "10")
BRAND_LINK_RE = re.compile(r'<a[^>]+href="/china/([a-z][a-z0-9]*)/#pvareaid=105866[^"]*"[^>]*>([^<]{1,20})</a>')


def series_key(name: str) -> str:
    """Модель по названию карточки: «奥迪A4L 2022款 40 TFSI» → «奥迪A4L»."""
    return re.split(r"\d{4}\s*款", name, maxsplit=1)[0].strip() or name


SCAN_WORKERS = int(os.environ.get("CHE168_SCAN_WORKERS") or "4")


def _norm(text: str) -> str:
    return re.sub(r"[\s·\-()（）]", "", text or "").lower()


def is_real_list(html: str) -> bool:
    """Настоящая страница списка che168 (с карточками или честно пустая) — в отличие от
    заглушки защиты от ботов: у настоящей есть фильтры и список марок."""
    return bool(parse_cards(html)) or "moredisplace" in html or bool(BRAND_LINK_RE.search(html))


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
        time.sleep(random.uniform(0.5, 2.0))
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

    get, close_all = browser_fetcher()
    pool = ThreadPoolExecutor(SCAN_WORKERS)
    groups, seen = {}, set()
    stats = {"ok": 0, "empty": 0, "failed": 0, "blocked": 0}
    saved = set()

    def add_cards(content):
        cards = parse_cards(content)
        learn_brands(cards)
        for c in cards:
            if c["infoid"] in seen or not c["year"] or c["year"] < MIN_YEAR:
                continue
            seen.add(c["infoid"])
            if c["infoid"] in known:
                # Уже на сайте с полной информацией — только отметка «ещё в продаже»
                if touched is not None:
                    touched.append(c)
                continue
            make = brand_model(c["name"], "", c.get("brandid"))[0]
            c["power"] = power_class(c["name"], make) if make else None
            if c["power"]:
                groups.setdefault(series_key(c["name"]), []).append(c)
        return cards

    def run(jobs, label, on_page):
        """jobs — [(url, данные)]; на каждую скачанную страницу — on_page(html, данные)."""
        done = 0
        futures = {pool.submit(get, url): (url, extra) for url, extra in jobs}
        for fut in as_completed(futures):
            url, extra = futures[fut]
            try:
                got = fut.result()
            except Exception as error:
                print(f"  {label}: {url} — {str(error).splitlines()[0][:120]}")
                got = None
            done += 1
            if got == "blocked":
                stats["blocked"] += 1
            elif not got:
                stats["failed"] += 1
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
                      f"пустых {stats['empty']}, ошибок {stats['failed']}, проверок {stats['blocked']}")
            if time.time() > deadline or stats["blocked"] >= 10:
                for x in futures:
                    x.cancel()
                why = "время обхода вышло" if time.time() > deadline else "che168 начал показывать проверку"
                print(f"  {label}: {why} — обработано {done} из {len(jobs)}")
                return False
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

    run([(f"https://www.che168.com/china/{slug}/", (slug, name)) for slug, name in brands], "марки", on_brand)
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
    if jobs and time.time() < deadline:
        run(jobs, "модели", lambda content, name: add_cards(content))
    close_all(pool, SCAN_WORKERS)
    pool.shutdown(wait=True)
    print(f"Обход за {(time.time() - started) / 60:.0f} мин: моделей с машинами {len(groups)}, "
          f"машин {sum(map(len, groups.values()))}; страниц с машинами {stats['ok']}, пустых {stats['empty']}, "
          f"ошибок {stats['failed']}, проверок {stats['blocked']}; запросом {get.stats['request']}, "
          f"через загрузку страницы {get.stats['browser']}")
    return groups


# Доли по годам: 70% — 2022–2024, 15% — 2025–2026, 15% — 2017–2021 (порядок — приоритет)
YEAR_BANDS = [("2022–2024", 2022, 2024, 0.70), ("2025–2026", 2025, 2026, 0.15), ("2017–2021", 2017, 2021, 0.15)]


def year_band(year):
    return next((name for name, lo, hi, _ in YEAR_BANDS if year and lo <= year <= hi), None)


def pick_models(groups: dict, total: int, on_site: dict | None = None) -> list[dict]:
    """Не больше total машин: 75% до 160 л.с., 25% мощнее (электро, гибриды), по годам — YEAR_BANDS.

    1) каждая модель, у которой есть машина до 160 л.с., — одна такая машина;
    2) добор «до 160» по кругу по моделям до 75%, внутри — по долям лет;
    3) модели, где есть только мощные, — по одной, сколько влезает в 25%. Их порядок
       случайный: от прогона к прогону на сайт попадают разные, а не попавшие через
       20 дней скрываются — со временем на сайте бывают все модели;
    4) добор мощных до 25%.
    Модели, которых на сайте меньше (on_site), идут первыми; по машине без очереди (1 и 3)
    получают только модели, которых на сайте ещё нет.
    """
    on_site = on_site or {}
    order = sorted(groups, key=lambda k: (on_site.get(k, 0), random.random()))
    groups = {k: groups[k] for k in order}
    picked, used = [], set()
    count = {}
    rank = {name: i for i, (name, *_) in enumerate(YEAR_BANDS)}
    quota = {"le160": round(total * SHARE_160)}
    quota["other"] = total - quota["le160"]

    def take(c):
        picked.append(c)
        used.add(c["infoid"])
        k = (c["power"], year_band(c["year"]))
        count[k] = count.get(k, 0) + 1

    def total_of(kind):
        return sum(v for (k, _), v in count.items() if k == kind)

    for cars in groups.values():
        # Сначала 2022–2024, потом 2025–2026, потом старше; уже на сайте — первыми (порядок сохраняется)
        cars.sort(key=lambda c: rank.get(year_band(c["year"]), 9))
    le_models = [cars for k, cars in groups.items() if any(c["power"] == "le160" for c in cars) and not on_site.get(k)]
    other_models = [cars for k, cars in groups.items() if not any(c["power"] == "le160" for c in cars)
                    and not on_site.get(k)]
    for cars in le_models:
        if total_of("le160") < quota["le160"]:
            take(next(c for c in cars if c["power"] == "le160"))

    def fill(kind, band, need):
        pools = {k: iter([c for c in v if c["power"] == kind and (band is None or year_band(c["year"]) == band)])
                 for k, v in groups.items()}
        progress = True
        while need() and progress:
            progress = False
            for it in pools.values():
                if not need():
                    break
                c = next((c for c in it if c["infoid"] not in used), None)
                if c:
                    take(c)
                    progress = True

    def fill_kind(kind, target):
        for name, _, _, w in YEAR_BANDS:
            want = round(target * w)
            fill(kind, name, lambda: count.get((kind, name), 0) < want and total_of(kind) < target)
        fill(kind, None, lambda: total_of(kind) < target)   # в какой-то группе лет машин не хватило

    fill_kind("le160", quota["le160"])
    ratio = (1 - SHARE_160) / SHARE_160 if SHARE_160 else 0
    # Мощных — не больше трети от «до 160» (если «до 160» не хватило, мощных тоже меньше)
    other_target = min(quota["other"], math.floor(total_of("le160") * ratio))
    for cars in other_models:
        if total_of("other") < other_target:
            take(cars[0])
    covered = len({series_key(c["name"]) for c in picked})
    fill_kind("other", other_target)
    share = total_of("le160") / len(picked) if picked else 0
    years = {name: sum(v for (_, b), v in count.items() if b == name) for name, *_ in YEAR_BANDS}
    print(f"Выбрано: моделей {covered} из {len(groups)} (новых на сайте: с машинами до 160 л.с. {len(le_models)}, только мощные "
          f"{len(other_models)}), машин {len(picked)} — до 160 л.с. {total_of('le160')} ({share:.0%}), "
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

    def kw(self, specid):
        if not specid or specid == "0":
            return None
        if specid in self.cache:
            self.stats["cache"] += 1
            return self.cache[specid]
        time.sleep(random.uniform(1.0, 2.5))
        kw = None
        try:
            resp = self.s.get(f"https://www.autohome.com.cn/spec/{specid}/", timeout=30)
            if resp.status_code == 200:
                m = re.search(r">\s*(\d{2,4}(?:\.\d)?)\s*kW\s*</div>\s*<div>\s*最大功率", resp.text)
                kw = float(m.group(1)) if m else None
        except Exception:
            pass
        self.stats["fetched" if kw else "missing"] += 1
        if kw is not None:
            self.cache[specid] = kw   # не нашли — не запоминаем, в следующий раз попробуем снова
        return kw

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.cache, f, ensure_ascii=False, indent=0, sort_keys=True)


def drom_car(car: dict, make: str, model: str) -> dict:
    """Машина che168 → признаки для поиска комплектации на drom.ru."""
    import drom_specs
    name = car["name"]
    model_year = re.search(r"(20\d{2})\s*款", name)
    fuel = fuel_of(name, "")
    return {
        "make": make, "model": model, "market": "china",
        "year": int(model_year.group(1)) if model_year else car["year"],
        "cc": guess_cc(name) if fuel != "Электро" else None,
        "fuel": drom_specs.norm_fuel(fuel), "drive": drom_specs.norm_drive(name),
        "trans": drom_specs.norm_trans(name), "trim": latin_trim(name),
    }


def add_power(spec: dict, car: dict, make, model, autohome, drom, counts):
    """Точная мощность в характеристики: Autohome по номеру комплектации, иначе drom.ru."""
    if spec.get("Максимальная мощность (кВт)") or spec.get("Мощность, л.с."):
        return
    kw = autohome.kw(car.get("specid")) if autohome else None
    if kw:
        spec["Максимальная мощность (кВт)"] = f"{kw:g}"
        counts["autohome"] += 1
        return
    found = drom.power(drom_car(car, make, model)) if (drom and make and model) else None
    if found:
        spec["Мощность, л.с."] = str(found["hp"])
        if found.get("hp_total"):
            spec["Суммарная мощность гибрида, л.с."] = str(found["hp_total"])
        counts["drom"] += 1
        return
    counts["none"] += 1


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


def fetch_known() -> dict:
    """Все объявления che168 на сайте: id → марка, модель, complete (фото, цена, данные для расчёта)…"""
    if not BN_AUTO_URL or not BN_AUTO_IMPORT_TOKEN:
        return {}
    try:
        resp = requests.get(f"{BN_AUTO_URL}/api/live-listings/known", params={"source": "che168", "all": "1"},
                            headers={"Authorization": f"Bearer {BN_AUTO_IMPORT_TOKEN}"}, timeout=60)
        resp.raise_for_status()
        return {str(i["id"]): i for i in resp.json().get("items") or []}
    except Exception as error:
        print(f"Список известных объявлений не получен ({error}) — разбираем всё")
        return {}


def good_known(items: dict) -> set:
    """Машины с сайта, которые заново не открываем: полная информация и крупное фото.
    Остальные (без фото, модели, цены, двигателя или с миниатюрой) — загрузим заново."""
    ids = {k for k, i in items.items() if i.get("complete", True) and (i.get("photo_kb") or 999) >= 25}
    print(f"На сайте: {len(items)}, с полной информацией {len(ids)} — их объявления не открываем; "
          f"неполные ({len(items) - len(ids)}) загрузим заново, если встретятся")
    return ids


def run_size(items: dict) -> int:
    """Сколько новых машин добавить: вручную (CHE168_TOTAL), до заполнения каталога
    (FILL_TARGET) — по FILL_PER_RUN за прогон, потом раз в неделю WEEKLY_NEW; 0 — сегодня не нужно."""
    if TOTAL:
        return TOTAL
    good = sum(1 for i in items.values() if i.get("complete") and i.get("published"))
    if good < FILL_TARGET:
        n = min(FILL_PER_RUN, FILL_TARGET - good)
        print(f"Заполнение каталога: на сайте {good} из {FILL_TARGET} — добавим {n}")
        return n
    if time.gmtime().tm_wday == WEEKLY_DAY:
        print(f"Каталог заполнен ({good}) — еженедельное обновление: до {WEEKLY_NEW} новых")
        return WEEKLY_NEW
    print(f"Каталог заполнен ({good}), сегодня не день обновления")
    return 0


def complete(x: dict) -> bool:
    """Полная информация: фото, цена, год, марка, модель и то, по чему считается таможня —
    объём (мощность сайт оценит по нему), у электромобилей — мощность."""
    spec = x.get("spec") or {}
    ev = spec.get("Тип топлива") == "Электро"
    engine = (spec.get("Максимальная мощность (кВт)") or spec.get("Мощность, л.с.")) if ev \
        else spec.get("Рабочий объём цилиндров (см³)")
    return bool(x.get("photo_url") and x.get("price_value") and x.get("year") and x.get("make")
                and x.get("model") and engine)


def push(listings):
    full_count = sum(1 for x in listings if "spec" in x)
    listings = [x for x in listings if "spec" not in x or complete(x)]
    if full_count > sum(1 for x in listings if "spec" in x):
        print(f"Не отправлены без фото, цены, модели или двигателя: {full_count - sum(1 for x in listings if 'spec' in x)}")
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
            return pick_models(groups, total, on_site)
    return collect(page, known, total)


def main():
    started = time.time()
    items = fetch_known()
    known = good_known(items)
    total = run_size(items)
    if not total:
        return
    touched = []
    session = http_session()
    listings = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        # Обход всех моделей — сотни страниц списка: их che168 отдаёт и напрямую, и так
        # быстрее; китайский прокси — для объявлений (переключаемся на капче, см. ниже)
        list_proxy = USE_PROXY and not ALL_MODELS
        on_proxy = list_proxy
        context = new_context(browser, list_proxy)
        page = context.new_page()
        cars = gather(page, known, total, items, touched)
        if not cars and list_proxy:
            # Прокси не отвечает (бесплатные быстро умирают) — список пробуем напрямую
            print("Через прокси список не получен — пробуем напрямую")
            context.close()
            on_proxy = False
            context = new_context(browser, False)
            page = context.new_page()
            cars = gather(page, known, total, items, touched)
        elif not cars and PROXY_SERVER:
            # Напрямую che168 не отдал список — пробуем через прокси
            print("Напрямую список не получен — пробуем через прокси")
            context.close()
            on_proxy = True
            context = new_context(browser, True)
            page = context.new_page()
            cars = gather(page, known, total, items, touched)
        print(f"Отобрано новых: {len(cars)}, машин с сайта встречено в обходе: {len(touched)}, марок по номерам: {len(BRAND_IDS)}")
        # Отметка «ещё в продаже» машинам с сайта, встреченным в обходе
        push([{"external_id": c["infoid"], "price_value": c["price_cny"], "mileage_km": c["mileage_km"],
               "source_url": f"https://www.che168.com/dealer/{c['dealerid']}/{c['infoid']}.html"}
              for c in {c["infoid"]: c for c in touched}.values()])

        # Точная мощность: Autohome по номеру комплектации, запасной путь — каталог drom.ru
        # (его таблицы дорисовываются скриптом — открываем в браузере, без прокси)
        import drom_specs
        autohome = AutohomePower(os.path.join(ROOT, "autohome_cache.json"))
        drom_context = browser.new_context(user_agent=UA, locale="ru-RU", timezone_id="Asia/Vladivostok")
        drom = drom_specs.DromCatalog(os.path.join(ROOT, "drom_cache.json"), drom_specs.playwright_fetcher(drom_context),
                                      max_requests=int(os.environ.get("DROM_MAX_PAGES") or "300"))
        power_counts = {"autohome": 0, "drom": 0, "none": 0}

        done = failed = degraded = degraded_saved = from_list = 0
        list_only = False   # объявления закрыты капчей — дальше только данные списка
        fails_in_row = 0
        breaks = random.randint(20, 30)

        def add(car, url, body=None, d=None):
            nonlocal done, from_list
            if d is None:
                d = {"spec": spec_from_name(car), "photos": []}
                from_list += 1
            make, model = brand_model(car["name"], body or "", car.get("brandid"))
            add_power(d["spec"], car, make, model, autohome, drom, power_counts)
            listings.append({
                "external_id": car["infoid"], "make": make, "model": model, "title": car["name"],
                "year": car["year"], "mileage_km": car["mileage_km"], "price_value": car["price_cny"],
                "photo_url": fetch_photo(session, d["photos"] + photo_candidates(car.get("image"))),
                "spec": d["spec"] or None, "source_url": url,
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
                print(f"=== Отправлено {idx} из {len(cars)} — пауза {BATCH_PAUSE:g} мин ===")
                time.sleep(BATCH_PAUSE * 60)
                list_only = False   # после паузы ещё раз пробуем открыть объявления
                fails_in_row = 0
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
                fails_in_row += 1
                print(f"[{car['infoid']}] не открылось: {str(error).splitlines()[0][:150]} — берём данные из списка")
                if fails_in_row >= 2:
                    # che168 перестал отдавать объявления (подвисает) — не ждём на каждой машине
                    print("Объявления не открываются второй раз подряд — дальше только данные списка")
                    list_only = True
                add(car, url)
                continue
            fails_in_row = 0
            if degraded_saved < 5 and done < 2:
                save_debug(f"detail_{car['infoid']}.html", body)
            add(car, url, body, parse_detail(body, car))
            human_pause(3, 7)
            if done >= breaks:
                human_pause(25, 45)
                breaks = done + random.randint(20, 30)
        autohome.save()
        drom.save()
        print(f"Мощность: из Autohome {power_counts['autohome']}, из drom.ru {power_counts['drom']} "
              f"(совпадений drom.ru: {drom.stats}, страниц drom.ru {drom.requests}), не найдена {power_counts['none']}; "
              f"Autohome: {autohome.stats}")
        browser.close()

    print(f"Итого: новых {sum('spec' in x for x in listings)} (из них только по списку {from_list}), "
          f"уже на сайте {sum('spec' not in x for x in listings)}, урезанных страниц {degraded}, ошибок {failed}")
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump([{k: v for k, v in x.items() if k != "photo_url"} for x in listings], f, ensure_ascii=False, indent=2)
    push(listings[pushed:])


if __name__ == "__main__":
    main()
