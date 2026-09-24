"""
Проверка Autohome (3): полный список опций страницы конфигурации с расшифрованными названиями
(часть символов в названиях спрятана в CSS ::before — берём их из отрисованной страницы).
Печатает «номер | группа | название | значение» — по ним составляется соответствие номеров
опций пунктам блока «Комплектация» на сайте. Ничего не отправляет.
"""

import json
import re

from playwright.sync_api import sync_playwright

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")
SPECS = ["64452", "60345", "55680"]
SPAN = re.compile(r"<span class='(hs_kw\d+_\w+)'></span>")


def var_json(html, name):
    m = re.search(rf"var {name}\s*=\s*(\{{.*?\}});\s*\n", html, re.S)
    if not m:
        m = re.search(rf"var {name}\s*=\s*(\{{.*?\}});", html, re.S)
    return json.loads(m.group(1)) if m else None


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=UA, locale="zh-CN")
        for n, spec in enumerate(SPECS):
            try:
                page.goto(f"https://car.autohome.com.cn/config/spec/{spec}.html", wait_until="domcontentloaded", timeout=90000)
            except Exception as e:
                print(f"spec {spec}: {e}")
                continue
            page.wait_for_timeout(8000)
            html = page.content()
            raw = page.evaluate("() => document.documentElement.outerHTML")
            kw = page.evaluate("""() => {
                const out = {};
                document.querySelectorAll("span[class^='hs_kw']").forEach((el) => {
                    if (out[el.className] !== undefined) return;
                    out[el.className] = getComputedStyle(el, '::before').content;
                });
                return out;
            }""")
            kw = {k: v.strip('"') if v and v != "none" else "" for k, v in kw.items()}
            print(f"\n######## spec {spec}: html {len(html)}, классов-заглушек в DOM {len(kw)}")
            src = None
            for s in page.evaluate("() => [...document.scripts].map(s => s.text)"):
                if "var option" in s:
                    src = s
                    break
            opt = var_json(src or raw, "option")
            if not opt:
                print("var option не найден")
                continue
            dec = lambda s: SPAN.sub(lambda m: kw.get(m.group(1), "?"), s or "")
            for group in opt["result"]["configtypeitems"]:
                print(f"=== группа: {dec(group.get('name'))}")
                for it in group["configitems"]:
                    v = next((x for x in it["valueitems"] if str(x["specid"]) == spec), it["valueitems"][0])
                    sub = "; ".join(f"{dec(s.get('subname'))}={s.get('subvalue')}" for s in v.get("sublist") or [])
                    value = dec(v.get("value")).replace("&nbsp;", " ")
                    if n == 0:
                        print(f"{it['id']} | {dec(it['name'])} | {value} | {sub}")
                    else:
                        print(f"{it['id']}={value}{' [' + sub + ']' if sub else ''}", end="  ")
                print()
        browser.close()


if __name__ == "__main__":
    main()
