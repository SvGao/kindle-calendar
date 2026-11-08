"""Build app/data/quotes.json from the chinese-poetry corpus.

Run once (and again whenever you want to refresh the pool):

    python tools/build_quotes.py <path-to-raw-json-dir>

Expected raw files in that dir:
    song/ci.song.*.json      全宋词 (simplified)
    song300.json             宋词三百首 (simplified)
    tang300.json             唐诗三百首 (traditional -> converted)

Only short, common-character couplets survive, so the footer never shows a
line nobody can read.
"""

from __future__ import annotations

import gzip
import json
import random
import re
import sys
from pathlib import Path

from opencc import OpenCC

OUT = Path(__file__).resolve().parent.parent / "app" / "data" / "quotes.json.gz"
LIMIT = 9000

# Poets whose lines are worth showing more often.
FAMOUS = {
    "苏轼", "辛弃疾", "李清照", "柳永", "晏殊", "晏几道", "欧阳修", "周邦彦",
    "姜夔", "秦观", "贺铸", "张先", "范仲淹", "陆游", "岳飞", "朱敦儒",
    "张孝祥", "陈亮", "刘过", "史达祖", "吴文英", "蒋捷", "张炎", "王沂孙",
    "周密", "黄庭坚", "晁补之", "叶梦得", "李之仪", "张元干", "刘辰翁",
    "文天祥", "王观", "毛滂", "刘克庄", "吴潜",
    "李白", "杜甫", "王维", "白居易", "李商隐", "杜牧", "孟浩然", "王昌龄",
    "刘禹锡", "岑参", "韩愈", "柳宗元", "李贺", "元稹", "张九龄", "韦应物",
    "王之涣", "王勃", "张继", "温庭筠", "李煜", "王翰", "常建", "崔颢",
}

SPLIT = re.compile(r"[，。！？；、,.!?;：:\s]+")
ALLOWED_PUNCT = "，"

# Panegyric and birthday-dedication verse is technically fine but reads as
# flattery on a wall; keep it out of the pool.
BAN = (
    "圣君", "皇图", "大业", "升平", "尧舜", "万岁", "千秋", "称觞",
    "鹤算", "龟龄", "椿龄", "眉寿", "华诞", "祝寿", "圣寿", "天颜",
    "宸", "御", "恩荣", "荣遇", "灵椿", "寿觞", "称寿",
    # 楚辞/诗经 style particles read as archaic on a wall clock
    "兮", "矣",
)

# Scene tags drive the seasonal / festival / time-of-day rotation in app/quotes.py.
# Keep the words concrete: a tag that matches half the corpus is useless as a filter.
TAG_WORDS: dict[str, tuple[str, ...]] = {
    "spring": ("春", "燕", "莺", "桃", "杏", "柳", "蝶", "草", "绿", "梨花", "海棠"),
    "summer": ("荷", "莲", "蝉", "暑", "夏", "池", "竹", "凉", "蒲"),
    "autumn": ("秋", "菊", "雁", "枫", "叶落", "西风", "茱萸", "重阳"),
    "winter": ("雪", "寒", "霜", "冰", "梅", "炉", "冬", "腊", "冻", "暖阁", "岁寒"),
    "springfest": ("爆竹", "屠苏", "桃符", "守岁", "岁除", "元日", "新桃", "春联", "椒盘",
                   "除夕", "年夜", "新年", "贺岁", "拜年", "椒花", "椒柏", "除夜", "岁夜"),
    "lantern": ("元宵", "花灯", "上元", "灯火", "灯市", "灯夕", "火树", "银花", "鳌山",
                "看灯", "灯夜", "烧灯", "灯期"),
    "qingming": ("清明", "寒食", "纸钱", "墓", "踏青", "杏花村", "禁烟", "冷食"),
    "duanwu": ("端午", "粽", "龙舟", "菖蒲", "五丝", "彩线", "浴兰", "重午", "竞渡", "艾虎", "艾人"),
    "midautumn": ("中秋", "婵娟", "团圆", "嫦娥", "玉兔", "冰轮", "玉盘", "银蟾",
                  "素娥", "月华", "赏月", "桂花", "桂香", "桂影", "桂子", "丹桂",
                  "桂华", "桂魄", "蟾桂"),
    "chongyang": ("重阳", "登高", "茱萸", "菊花", "黄花", "九日", "重九", "菊"),
    "morning": ("晓", "晨", "朝来", "早起", "日出", "鸡", "清晓", "破晓", "睡起", "早", "晓妆"),
    "night": ("夜", "月", "灯", "烛", "梦", "更深", "独", "静", "眠", "漏", "枕", "窗"),
}


def tags_for(text: str) -> list[str]:
    return [k for k, words in TAG_WORDS.items() if any(w in text for w in words)]


# Known transcription errors in the upstream corpus. 苏轼《卜算子》 ends with
# 「寂寞沙洲冷」; the dataset has 崔信明's 残句「枫落吴江冷」 spliced in, which
# reads as nonsense on the wall.
FIXUPS = {
    "拣尽寒枝不肯栖，枫落吴江冷": "拣尽寒枝不肯栖，寂寞沙洲冷",
}


# Only the handful of 国风 pieces whose lines stand on their own today.
SHIJING_KEEP = {
    "关雎", "蒹葭", "桃夭", "击鼓", "子衿", "采葛", "木瓜", "静女",
    "风雨", "月出", "氓", "君子于役", "硕鼠", "伐檀",
}


def is_common(ch: str) -> bool:
    """True for GB2312 level-1 hanzi (the ~3755 characters people actually read)."""
    if "\u4e00" <= ch <= "\u9fff":
        try:
            b = ch.encode("gb2312")
        except UnicodeEncodeError:
            return False
        return len(b) == 2 and 0xB0 <= b[0] <= 0xD7
    return True


def clean(parts: list[str]) -> str | None:
    """Join two clauses into one readable couplet, or reject it."""
    a, b = parts
    if not (4 <= len(a) <= 11 and 4 <= len(b) <= 11):
        return None
    text = f"{a}，{b}"
    if not (8 <= len(text) - 1 <= 20):
        return None
    hanzi = [c for c in text if "\u4e00" <= c <= "\u9fff"]
    if len(hanzi) < 8:
        return None
    common = sum(1 for c in hanzi if is_common(c))
    if common / len(hanzi) < 1.0:
        return None
    # Drop lines with latin digits, brackets, or unknown glyphs.
    if any(not ("\u4e00" <= c <= "\u9fff" or c == "，") for c in text):
        return None
    return text


def clauses(paragraphs: list[str]) -> list[str]:
    out: list[str] = []
    for p in paragraphs:
        for piece in SPLIT.split(p):
            piece = piece.strip()
            if piece:
                out.append(piece)
    return out


def couples(paragraphs: list[str]) -> list[str]:
    """Only take a *whole* line unit (``A，B。``), never a slice out of context.

    Cutting mid-clause is what produces lines like 「辇路重来，仿佛灯前事」 that
    read as if they were truncated, so a paragraph must end a sentence and carry
    exactly two clauses to qualify.
    """
    res: list[str] = []
    for p in paragraphs:
        p = p.strip()
        if not p or p[-1] not in "。！？!?":
            continue
        body = p.rstrip("。！？!?")
        parts = [x.strip() for x in re.split(r"[，、；,;]", body) if x.strip()]
        if len(parts) != 2:
            continue
        t = clean(parts)
        if t:
            res.append(t)
    return res


def lines_from_text(text: str) -> list[str]:
    """For 诗经 / 增广贤文 / 幽梦影: split on sentence marks, then on ；, then
    keep the two-clause units. Handles several couplets packed in one string."""
    res: list[str] = []
    for sent in re.split(r"[。！？!?\n]+", text):
        for chunk in re.split(r"[；;]", sent):
            parts = [x.strip() for x in re.split(r"[，、,]", chunk) if x.strip()]
            if len(parts) != 2:
                continue
            t = clean(parts)
            if t:
                res.append(t)
    return res


def main(raw_dir: Path) -> None:
    cc = OpenCC("t2s")
    seen: set[str] = set()
    rows: list[dict] = []

    def add(text: str, author: str, source: str, tier: int) -> None:
        text = FIXUPS.get(text, text)
        if text in seen:
            return
        if any(w in text for w in BAN):
            return
        seen.add(text)
        row = {"t": text, "a": author or "", "s": source or "", "k": tier}
        g = tags_for(text)
        if g:
            row["g"] = g
        rows.append(row)

    # Tier 1: hand-picked lines that always read well.
    curated_path = OUT.parent / "curated.json"
    curated_texts: set[str] = set()
    if curated_path.exists():
        for item in json.loads(curated_path.read_text(encoding="utf-8")):
            curated_texts.add(item["t"])
            add(item["t"], item.get("a", ""), item.get("src") or "精选", 1)

    # Tier 1: the two curated anthologies.
    for name, src, tier in (("song300.json", "rhythmic", 1), ("tang300.json", "title", 1)):
        data = json.loads((raw_dir / name).read_text(encoding="utf-8"))
        for item in data:
            author = cc.convert(item.get("author", ""))
            src_name = cc.convert(item.get(src, "") or "")
            for c in couples(item.get("paragraphs", [])):
                add(cc.convert(c), author, src_name, tier)

    # Tier 1: 诗经 / 幽梦影 / 增广贤文 -- short classical lines and maxims.
    shijing = json.loads((raw_dir / "shijing.json").read_text(encoding="utf-8"))
    for item in shijing:
        if item.get("chapter") != "国风" or item.get("title") not in SHIJING_KEEP:
            continue
        title = cc.convert("《诗经》" + item.get("title", ""))
        for c in lines_from_text("\n".join(item.get("content", []))):
            add(cc.convert(c), "诗经", title, 1)

    youmeng = json.loads((raw_dir / "youmengying.json").read_text(encoding="utf-8"))
    for item in youmeng:
        for c in lines_from_text(item.get("content", "")):
            add(cc.convert(c), "张潮", "幽梦影", 1)

    # 增广贤文 is folk wisdom: keep the edifying half, drop the cynical half.
    BAD = ("小人", "是非", "祸", "灾", "贫", "贱", "钱财", "官司", "妻", "妾",
           "鬼", "死", "酒", "色", "横财", "命里", "人欺", "心狠")
    zeng = json.loads((raw_dir / "zengguang.json").read_text(encoding="utf-8"))
    for chapter in zeng.get("content", []):
        for c in lines_from_text("\n".join(chapter.get("paragraphs", []))):
            t = cc.convert(c)
            if any(w in t for w in BAD):
                continue
            add(t, "增广贤文", "增广贤文", 1)

    # Tier 2/3: the full Song corpus, famous poets first.
    for f in sorted((raw_dir / "song").glob("ci.song.*.json")):
        for item in json.loads(f.read_text(encoding="utf-8")):
            author = cc.convert(item.get("author", ""))
            if author in ("无名氏", "佚名", ""):
                continue  # unattributed lines are often corrupt or folk doggerel
            tier = 2 if author in FAMOUS else 3
            rhythmic = cc.convert(item.get("rhythmic", ""))
            for c in couples(item.get("paragraphs", [])):
                add(cc.convert(c), author, rhythmic, tier)

    print(f"collected {len(rows)} unique couplets")
    for k in (1, 2, 3):
        print(f"  tier{k}: {sum(1 for r in rows if r['k'] == k)}")

    tier1 = [r for r in rows if r["k"] == 1]
    tier2 = [r for r in rows if r["k"] == 2]
    tier3 = [r for r in rows if r["k"] == 3]
    random.seed(20261108)
    random.shuffle(tier2)
    random.shuffle(tier3)
    # Keep the curated anthology in full; sample the rest so the file stays small
    # and every line is one a reader can actually enjoy.
    # Hand-picked lines carry the most weight, then the anthologies, then the
    # long tail -- so most refreshes show something familiar.
    curated = [r for r in tier1 if r["t"] in curated_texts]
    rest1 = [r for r in tier1 if r["t"] not in curated_texts]
    # Festival lines are rare in a random sample but they are the whole point of
    # the seasonal rotation, so give them their own quota instead of letting the
    # shuffle drop them.
    fest = {"springfest", "lantern", "qingming", "duanwu", "midautumn", "chongyang"}
    fest2 = [r for r in tier2 if fest.intersection(r.get("g", ()))]
    plain2 = [r for r in tier2 if not fest.intersection(r.get("g", ()))]
    fest3 = [r for r in tier3 if fest.intersection(r.get("g", ()))]
    plain3 = [r for r in tier3 if not fest.intersection(r.get("g", ()))]
    keep = curated * 10 + rest1 * 5 + fest2 * 2 + plain2[:3000] + fest3[:400] + plain3[:200]
    random.shuffle(keep)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(keep, ensure_ascii=False, separators=(",", ":")).encode()
    with gzip.open(OUT, "wb", compresslevel=9) as fh:
        fh.write(payload)
    print(f"wrote {OUT} rows={len(keep)} raw={len(payload)/1024:.0f}KB gz={OUT.stat().st_size/1024:.0f}KB")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "."))
