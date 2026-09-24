"""
Проверка точной мощности с drom.ru на типичных машинах Кореи, Китая и Японии:
находит ли модель, поколение и группу комплектаций, какую мощность берёт и
почему не нашёл. Ничего не отправляет на сайт.
"""

import json
import os

from playwright.sync_api import sync_playwright

import drom_specs

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")
KR = ["south-korea"]
IMP = ["south-korea", "europe"]
CARS = [
    dict(make="Hyundai", model="Avante", markets=KR, year=2022, month=5, cc=1598, fuel="petrol", trans="auto", drive="fwd", trim="1.6 Smart"),
    dict(make="Kia", model="K5", markets=KR, year=2021, month=3, cc=1999, fuel="petrol", trans="auto", drive="fwd", trim="2.0 Prestige"),
    dict(make="Kia", model="Sorento", markets=KR, year=2022, month=8, cc=2151, fuel="diesel", trans="auto", drive="4wd", trim="2.2 Signature"),
    dict(make="Hyundai", model="Grandeur", markets=KR, year=2023, month=4, cc=2497, fuel="petrol", trans="auto", drive="fwd", trim="2.5 Calligraphy"),
    dict(make="Genesis", model="G80", markets=KR, year=2021, month=6, cc=2497, fuel="petrol", trans="auto", drive="rwd", trim="2.5T"),
    dict(make="Hyundai", model="Tucson", markets=KR, year=2022, month=2, cc=1598, fuel="hybrid", trans="auto", drive="fwd", trim="1.6 Hybrid"),
    dict(make="BMW", model="5 Series", markets=IMP, year=2021, month=9, cc=1998, fuel="petrol", trans="auto", drive="rwd", trim="520i M Sport"),
    dict(make="Mercedes-Benz", model="E-Class", markets=IMP, year=2021, month=5, cc=1991, fuel="petrol", trans="auto", drive="rwd", trim="E250"),
    dict(make="Geely", model="Monjaro", market="china", year=2023, cc=1969, fuel="petrol", drive="4wd", trim="2.0TD"),
    dict(make="Haval", model="H6", market="china", year=2021, cc=1497, fuel="petrol", trim="1.5T"),
    dict(make="Nissan", model="Sylphy", market="china", year=2022, cc=1598, fuel="petrol", trans="cvt", trim="1.6XE CVT"),
    dict(make="Honda", model="Fit", market="japan", year=2022, month=11, cc=1496, fuel="hybrid", drive="4wd", trim="e:HEV Home 4WD"),
    dict(make="Honda", model="Fit", market="japan", year=2022, month=11, cc=1496, fuel="hybrid", drive="fwd", trim="e:HEV Home"),
]


def main():
    cache = os.path.join(os.path.dirname(os.path.abspath(__file__)), "drom_cache.json")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        drom = drom_specs.DromCatalog(cache, drom_specs.playwright_fetcher(browser.new_context(user_agent=UA, locale="ru-RU")))
        for car in CARS:
            path = drom.model_path(car["make"], car["model"])
            res = drom.power(car)
            markets = car.get("markets") or [car["market"]]
            gens = {m: [(g["market"], g["from"], g["to"], len(g["groups"])) for g in drom.generations(path, m, car["year"])] if path else []
                    for m in markets}
            print(f"\n{car['make']} {car['model']} {car['year']} {car.get('cc')} {car.get('fuel')} {car.get('drive')}"
                  f" → {path} → {res}")
            print("   поколения:", json.dumps(gens, ensure_ascii=False))
            if path and not res:
                for m in markets:
                    for g in drom.generations(path, m, car["year"]):
                        for gr in g["groups"]:
                            print("   группа:", gr["text"], "| трим:", [t["name"] for t in gr["trims"]][:3],
                                  [t["from"] for t in gr["trims"]][:1])
        print("\nИтог:", drom.stats, "страниц:", drom.requests)
        browser.close()


if __name__ == "__main__":
    main()
