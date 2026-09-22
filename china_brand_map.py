"""
Best-effort normalisation of Dongchedi's Chinese listing titles into a
Latin brand name + a short model guess — mirrors scraper/brand_map.py
in the encar-parser-landing repo (same idea, different market/language).

A Dongchedi title looks like "奥迪Q3 2022款 35 TFSI 时尚动感型"
(brand + model code, glued together with no separator, then "<year>款"
and the trim in Chinese). There's no clean brand/model split in the
page, and a full translation needs an API this project doesn't have.
Instead we:
  1. match a known Chinese brand name at the start of the title and map
     it to its Latin name;
  2. guess the model from the first Latin/alphanumeric token left in
     the remainder — Chinese listings usually keep the model code in
     Latin (Q3, GLC300, CS75, Model Y); domestic models without a Latin
     code (e.g. BYD 宋PLUS mixes Chinese with "PLUS") fall back to that
     token or stay unset — the full original title is always kept too.
This is an approximation, not a translation.
"""

import re

BRAND_MAP = {
    # Китайские марки
    "比亚迪": "BYD",
    "吉利汽车": "Geely",
    "吉利": "Geely",
    "长城汽车": "Great Wall",
    "长城": "Great Wall",
    "哈弗": "Haval",
    "坦克": "Tank",
    "奇瑞汽车": "Chery",
    "奇瑞": "Chery",
    "星途": "Exeed",
    "捷途": "Jetour",
    "长安汽车": "Changan",
    "长安": "Changan",
    "广汽传祺": "GAC Trumpchi",
    "传祺": "Trumpchi",
    "荣威": "Roewe",
    "名爵": "MG",
    "五菱": "Wuling",
    "宝骏": "Baojun",
    "领克": "Lynk & Co",
    "极氪": "Zeekr",
    "几何汽车": "Geometry",
    "几何": "Geometry",
    "腾势": "Denza",
    "埃安": "Aion",
    "理想汽车": "Li Auto",
    "理想": "Li Auto",
    "蔚来": "Nio",
    "小鹏汽车": "Xpeng",
    "小鹏": "Xpeng",
    "岚图": "Voyah",
    "阿维塔": "Avatr",
    "红旗": "Hongqi",
    "一汽": "FAW",
    "东风风行": "Dongfeng Fengxing",
    "东风": "Dongfeng",
    "北京汽车": "BAIC",
    "北汽": "BAIC",
    "上汽大众": "SAIC-Volkswagen",
    "上汽通用": "SAIC-GM",
    "威马汽车": "WM Motor",
    "威马": "WM Motor",
    "零跑汽车": "Leapmotor",
    "零跑": "Leapmotor",
    "哪吒汽车": "Neta",
    "哪吒": "Neta",
    # Иностранные марки на китайском рынке
    "奥迪": "Audi",
    "大众": "Volkswagen",
    "丰田": "Toyota",
    "本田": "Honda",
    "日产": "Nissan",
    "别克": "Buick",
    "雪佛兰": "Chevrolet",
    "福特": "Ford",
    "现代": "Hyundai",
    "起亚": "Kia",
    "宝马": "BMW",
    "奔驰": "Mercedes-Benz",
    "沃尔沃": "Volvo",
    "保时捷": "Porsche",
    "特斯拉": "Tesla",
    "凯迪拉克": "Cadillac",
    "林肯": "Lincoln",
    "雷克萨斯": "Lexus",
    "英菲尼迪": "Infiniti",
    "马自达": "Mazda",
    "斯巴鲁": "Subaru",
    "三菱": "Mitsubishi",
    "铃木": "Suzuki",
    "捷豹": "Jaguar",
    "路虎": "Land Rover",
    "标致": "Peugeot",
    "雪铁龙": "Citroen",
    "菲亚特": "Fiat",
    "斯柯达": "Skoda",
    "讴歌": "Acura",
    "极星": "Polestar",
    "劳斯莱斯": "Rolls-Royce",
    "宾利": "Bentley",
    "玛莎拉蒂": "Maserati",
    "法拉利": "Ferrari",
    "兰博基尼": "Lamborghini",
    "阿斯顿马丁": "Aston Martin",
}

_BRAND_KEYS = sorted(BRAND_MAP.keys(), key=len, reverse=True)

_SKIP_TOKENS = {"WD", "4WD", "2WD", "AWD", "RWD", "FWD", "PLUS", "PRO", "MAX"}


def extract_brand_model(raw_title: str):
    """Return (brand, model_guess, remainder) from a raw Dongchedi title."""
    raw_title = (raw_title or "").strip()
    if not raw_title:
        return None, None, raw_title

    brand = None
    remainder = raw_title
    for key in _BRAND_KEYS:
        if raw_title.startswith(key):
            brand = BRAND_MAP[key]
            remainder = raw_title[len(key):].strip()
            break

    if brand is None:
        return None, None, raw_title

    # Год ("2022款") — не модель, вырезаем перед поиском токена модели.
    remainder_for_model = re.sub(r"\d{4}\s*款", " ", remainder)

    first_token = None
    first_digit_token = None
    for token in re.findall(r"[A-Za-z][A-Za-z0-9\-]{1,}", remainder_for_model):
        token = token.strip("-")
        if not token or token.upper() in _SKIP_TOKENS:
            continue
        if first_token is None:
            first_token = token
        if first_digit_token is None and any(ch.isdigit() for ch in token):
            first_digit_token = token
            break

    model = first_digit_token or first_token
    return brand, model, remainder
