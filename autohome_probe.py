"""
Проверка Autohome: страница конфигурации комплектации (car.autohome.com.cn/config/spec/<id>.html) —
как в ней лежат опции (люк, камера, круиз, подогрев…) и их значения («●» есть, «○» опция, «-» нет).
Печатает структуру данных. Ничего не отправляет.
"""

import json
import re

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")
NEEDLES = ["天窗", "倒车影像", "定速巡航", "座椅加热", "真皮", "全景", "电动后备厢", "无钥匙", "胎压", "车道偏离"]


def around(t, needle, before=400, after=1200):
    i = t.find(needle)
    return t[max(0, i - before): i + after] if i >= 0 else ""


def main():
    try:
        cache = json.load(open("autohome_cache.json", encoding="utf-8"))
    except Exception:
        cache = {}
    specs = ["64452"] + [k for k in cache][:2]
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": "https://www.autohome.com.cn/", "Accept-Language": "zh-CN,zh;q=0.9"})
    for spec in specs:
        for url in (f"https://car.autohome.com.cn/config/spec/{spec}.html",
                    f"https://www.autohome.com.cn/config/spec/{spec}.html"):
            try:
                r = s.get(url, timeout=30)
            except Exception as e:
                print(url, "ошибка", e)
                continue
            t = r.text
            print(f"\n######## {url}: HTTP {r.status_code}, {len(t)} символов, 安全验证: {'安全验证' in t}")
            vars_ = re.findall(r"var\s+(\w+)\s*=\s*[\{\[]", t)
            print("переменные JS:", vars_[:20])
            for m in re.finditer(r"<script[^>]*>(.*?)</script>", t, re.S):
                body = m.group(1)
                if any(n in body for n in NEEDLES):
                    print("--- script с опциями: длина", len(body), "начало:", body[:300].replace("\n", " "))
                    break
            for n in NEEDLES[:6]:
                a = around(t, n)
                if a:
                    print(f"--- вокруг «{n}» ---")
                    print(a.replace("\n", " "))
            if any(n in t for n in NEEDLES):
                break


if __name__ == "__main__":
    main()
