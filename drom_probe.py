"""
Проверка источников точной мощности:
  * drom.ru — HTML заголовка группы комплектаций на странице поколения и
    карточки поколения на странице рынка (чтобы разбирать их простыми запросами);
  * Autohome — страница комплектации по specid из карточки che168.
Всё нужное печатается в лог. Ничего не отправляет на сайт.
"""

import os
import re
import time

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")
S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9,zh-CN;q=0.8",
                  "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})


def get(url, **kw):
    time.sleep(2)
    try:
        r = S.get(url, timeout=40, **kw)
    except Exception as error:
        print(f"{url}: {error}")
        return None, ""
    enc = r.encoding if r.encoding and r.encoding.lower() not in ("iso-8859-1",) else r.apparent_encoding
    r.encoding = "gb18030" if (enc or "").lower() in ("gb2312", "gbk") else enc
    print(f"\n{url}: HTTP {r.status_code}, {len(r.content)} байт, кодировка {r.encoding}")
    return r.status_code, r.text


def around(html, needle, before=600, after=2200, n=1):
    out, start = [], 0
    for _ in range(n):
        i = html.find(needle, start)
        if i < 0:
            break
        out.append(html[max(0, i - before): i + after])
        start = i + after
    return "\n.....\n".join(out)


def main():
    print("######## drom.ru: рынок (карточки поколений)")
    _, html = get("https://www.drom.ru/catalog/honda/fit/japan/")
    print(around(html, "/g_", 1500, 2500))
    print("\n######## drom.ru: поколение (заголовок группы и комплектации)")
    gens = re.findall(r'href="(https://www\.drom\.ru/catalog/honda/fit/g_\d+_\d+/)"', html)
    print("поколения:", list(dict.fromkeys(gens))[:10])
    if gens:
        _, g = get(gens[0])
        print(around(g, "Двигатель:", 2500, 3500))
        print("--- title ---", re.findall(r"<title>(.*?)</title>", g, re.S)[:1])
        print("--- рынок ---", re.findall(r"Рынок сбыта:[^<]{0,80}", g)[:2])

    print("\n######## Autohome по specid (Audi RS7 41697, BMW X5 64452, WRX 67452)")
    for spec in ("64452", "67452"):
        for url in (f"https://www.autohome.com.cn/spec/{spec}/",
                    f"https://car.autohome.com.cn/config/spec/{spec}.html",
                    f"https://m.autohome.com.cn/spec/{spec}/",
                    f"https://m.autohome.com.cn/config/spec/{spec}.html"):
            code, t = get(url, headers={"Referer": "https://www.autohome.com.cn/"})
            title = re.findall(r"<title>(.*?)</title>", t, re.S)[:1]
            print("title:", title, "| 安全验证:", "安全验证" in t, "| 马力:", len(re.findall("马力", t)),
                  "| 最大功率:", len(re.findall("最大功率", t)))
            for needle in ("最大马力", "马力", "最大功率(kW)", "最大功率"):
                if needle in t:
                    print(f"--- вокруг «{needle}» ---")
                    print(around(t, needle, 300, 700))
                    break


if __name__ == "__main__":
    main()
