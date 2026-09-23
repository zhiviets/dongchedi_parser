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
    # Новые и недостающие марки (встречаются на che168)
    "小米": "Xiaomi", "问界": "AITO", "智己": "IM Motors", "深蓝汽车": "Deepal", "深蓝": "Deepal",
    "方程豹": "Fangchengbao", "仰望": "Yangwang", "极越": "Jiyue", "享界": "Stelato", "智界": "Luxeed",
    "尊界": "Maextro", "广汽埃安": "Aion", "昊铂": "Hyptec", "极狐": "Arcfox", "北京越野": "BAIC BJ", "北京": "BAIC",
    "猛士": "M-Hero", "魏牌": "WEY", "WEY": "WEY", "欧拉": "ORA", "长安启源": "Changan Qiyuan", "五菱汽车": "Wuling",
    "上汽大通": "Maxus", "大通": "Maxus", "江淮": "JAC", "江铃": "JMC", "东风风神": "Dongfeng Fengshen", "奔腾": "Bestune",
    "捷达": "Jetta", "MINI": "MINI", "smart": "smart", "吉利银河": "Geely Galaxy", "Jeep": "Jeep", "道奇": "Dodge",
    "克莱斯勒": "Chrysler", "路特斯": "Lotus", "迈凯伦": "McLaren", "iCAR": "iCAR", "奇瑞新能源": "Chery",
    "SWM斯威汽车": "SWM", "福田": "Foton", "开瑞": "Karry", "海马": "Haima", "捷尼赛思": "Genesis",
    "奇瑞风云": "Chery Fengyun", "风云": "Chery Fengyun",
}

_BRAND_KEYS = sorted(BRAND_MAP.keys(), key=len, reverse=True)

_SKIP_TOKENS = {"WD", "4WD", "2WD", "AWD", "RWD", "FWD", "PLUS", "PRO", "MAX"}

# Модели, которые в Китае пишут только иероглифами (часть до «2022款»)
SERIES_MAP = {
    # Fangchengbao (BYD): «豹5» → Leopard 5
    "豹": "Leopard",
    # Toyota
    "凯美瑞": "Camry", "卡罗拉": "Corolla", "雷凌": "Levin", "汉兰达": "Highlander", "亚洲龙": "Avalon",
    "荣放": "RAV4", "威兰达": "Wildlander", "皇冠陆放": "Crown Kluger", "皇冠": "Crown", "普拉多": "Land Cruiser Prado",
    "陆地巡洋舰": "Land Cruiser", "赛那": "Sienna", "格瑞维亚": "Granvia", "锋兰达": "Frontlander", "威驰": "Vios",
    "埃尔法": "Alphard", "威尔法": "Vellfire",
    # Honda
    "雅阁": "Accord", "思域": "Civic", "飞度": "Fit", "奥德赛": "Odyssey", "艾力绅": "Elysion", "冠道": "Avancier",
    "皓影": "Breeze", "缤智": "Vezel", "凌派": "Crider", "型格": "Integra", "思铂睿": "Spirior",
    # Nissan
    "轩逸": "Sylphy", "天籁": "Teana", "奇骏": "X-Trail", "逍客": "Qashqai", "楼兰": "Murano", "途达": "Terra",
    # Mazda
    "阿特兹": "Atenza",
    # Volkswagen
    "途观": "Tiguan", "帕萨特": "Passat", "迈腾": "Magotan", "速腾": "Sagitar", "朗逸": "Lavida", "宝来": "Bora",
    "途昂": "Teramont", "探岳": "Tayron", "探歌": "T-Roc", "高尔夫": "Golf", "凌渡": "Lamando", "威然": "Viloran",
    "揽境": "Talagon", "辉昂": "Phideon", "蔚领": "C-Trek",
    # Buick / Chevrolet
    "君越": "LaCrosse", "君威": "Regal", "英朗": "Excelle GT", "昂科威": "Envision", "昂科旗": "Enclave", "别克GL8": "GL8",
    "迈锐宝": "Malibu", "科鲁泽": "Cruze",
    # Hyundai / Kia
    "索纳塔": "Sonata", "伊兰特": "Elantra", "途胜": "Tucson", "胜达": "Santa Fe", "帕里斯帝": "Palisade",
    "索兰托": "Sorento", "嘉华": "Carnival", "狮铂拓界": "Sportage",
    # Land Rover / Porsche
    "揽胜运动": "Range Rover Sport", "揽胜极光": "Range Rover Evoque", "揽胜星脉": "Range Rover Velar", "揽胜": "Range Rover",
    "发现运动": "Discovery Sport", "发现": "Discovery", "卫士": "Defender",
    "卡宴": "Cayenne", "帕拉梅拉": "Panamera",
    # BYD
    "汉": "Han", "唐": "Tang", "宋": "Song", "秦": "Qin", "元": "Yuan", "海豹": "Seal", "海豚": "Dolphin",
    "海鸥": "Seagull", "海狮": "Sealion", "护卫舰": "Frigate", "驱逐舰": "Destroyer",
    # Geely
    "星越": "Xingyue", "博越": "Boyue", "帝豪": "Emgrand", "星瑞": "Preface", "缤越": "Coolray", "豪越": "Okavango",
    "银河": "Galaxy",
    # Changan / Chery / Haval / GAC / Great Wall
    "逸动": "Eado", "欧尚": "Oshan", "深蓝": "Deepal", "启源": "Qiyuan", "瑞虎": "Tiggo", "艾瑞泽": "Arrizo",
    "大狗": "Dargo", "枭龙": "Xiaolong", "猛龙": "Menglong", "影豹": "Empow", "炮": "Poer",
    "夏": "Xia", "汉L": "Han L", "唐L": "Tang L", "宋L": "Song L",
    # Ford / Jeep / Cadillac / Lincoln / импорт
    "福克斯": "Focus", "蒙迪欧": "Mondeo", "锐界": "Edge", "探险者": "Explorer", "野马": "Mustang", "游骑侠": "Ranger",
    "撼路者": "Everest", "领界": "Territory", "锐际": "Escape", "大切诺基": "Grand Cherokee", "牧马人": "Wrangler",
    "自由光": "Cherokee", "指南者": "Compass", "角斗士": "Gladiator", "凯雷德": "Escalade", "领航员": "Navigator",
    "飞行家": "Aviator", "航海家": "Nautilus", "冒险家": "Corsair", "途锐": "Touareg", "辉腾": "Phaeton",
    "霸道": "Land Cruiser Prado", "红杉": "Sequoia", "坦途": "Tundra", "塞纳": "Sienna", "艾尔法": "Alphard",
    "途乐": "Patrol", "贵士": "Quest", "雅阁锐·混动": "Accord Hybrid",
    # BYD / Geely и др. без латиницы
    "星愿": "Xingyuan", "熊猫": "Panda", "缤瑞": "Binrui",
}
_SERIES_KEYS = sorted(SERIES_MAP.keys(), key=len, reverse=True)


def _series_model(series: str):
    """Модель из части названия до «款»: словарь, «E级» → E-Class, «5系» → 5 Series, латиница."""
    series = series.strip()
    for key in _SERIES_KEYS:
        if series.startswith(key):
            name = SERIES_MAP[key]
            tail = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9\-]*)", series[len(key):])
            # «宋PLUS» → Song PLUS, «途观L» → Tiguan L; «凯雷德ESCALADE» — не повторяем название
            if tail and len(tail.group(1)) <= 5 and tail.group(1).lower() not in name.lower():
                name += f" {tail.group(1)}"
            return name
    m = re.match(r"([A-Z]{1,3})级", series)
    if m:
        return f"{m.group(1)}-Class"
    m = re.match(r"(\d)系", series)
    if m:
        return f"{m.group(1)} Series"
    # «RS 7», «Model 3», «ID. 4» — число через пробел тоже часть названия
    for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-\.]*(?:\s+(?:\d+|[A-Z])\b)?", series):
        if token.upper() not in _SKIP_TOKENS:
            return token.strip(".")
    return None


def extract_brand_model(raw_title: str):
    """Return (brand, model_guess, remainder) from a raw Dongchedi / che168 title."""
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

    # Модель — в части до года ("2022款"); после него идёт комплектация
    series = re.split(r"\d{4}\s*款", remainder, maxsplit=1)[0]
    model = _series_model(series)
    if model or re.search(r"\d{4}\s*款", remainder):
        # Модель не распознана, но год в названии есть — латиница после него
        # это комплектация («45 TFSI», «RS»), а не модель: лучше оставить пусто
        return brand, model, remainder

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
