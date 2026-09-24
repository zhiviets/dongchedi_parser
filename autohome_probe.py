"""
Проверка Autohome (2): где на странице конфигурации лежат значения опций («●» есть, «○» опция,
«-» нет) и как они связаны с номерами опций (keyLink id). Ничего не отправляет.
"""

import json
import re

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")


def main():
    cache = json.load(open("autohome_cache.json", encoding="utf-8"))
    specs = ["64452", list(cache)[0]]
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": "https://www.autohome.com.cn/", "Accept-Language": "zh-CN,zh;q=0.9"})
    for spec in specs:
        t = s.get(f"https://car.autohome.com.cn/config/spec/{spec}.html", timeout=30).text
        print(f"\n######## spec {spec}: {len(t)} символов; «●»: {t.count('●')}, «○»: {t.count('○')}")
        for name in ("var config", "var option", "var bag", "var color", "var innerColor", "configtypeitems",
                     "paramtypeitems", "valueitems", "specid", "\"value\""):
            i = t.find(name)
            print(f"--- «{name}»: позиция {i}, всего {t.count(name)}")
            if i >= 0 and name in ("var config", "var option", "configtypeitems", "valueitems"):
                print(t[i: i + 1500].replace("\n", " "))
        # Кусок с опцией «倒车影像»/«定速巡航» вместе со значением
        for needle in ("倒车", "巡航", "加热"):
            for m in list(re.finditer(needle, t))[:2]:
                print(f"--- вокруг «{needle}» @{m.start()} ---")
                print(t[max(0, m.start() - 200): m.start() + 500].replace("\n", " "))
        # Скрипты, которые подменяют span-заглушки
        for m in re.finditer(r"hs_kw\d+_\w+", t):
            print("--- первая заглушка:", m.group(0), "; стиль рядом:", t[max(0, m.start() - 100): m.start() + 200].replace("\n", " "))
            break
        js = re.findall(r'<script[^>]+src="([^"]+)"', t)
        print("внешние скрипты:", js[:25])
        break_after = spec  # второй spec — для сравнения


if __name__ == "__main__":
    main()
