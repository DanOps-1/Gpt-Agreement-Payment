"""算法化人名 + 邮箱前缀生成器（音节合成法）。

设计目标：
  1. **不依赖固定姓名表**——通过音节（onset+vowel+coda+ending）按英语
     拼写规则随机组装，理论上可生成 10 万+ 新名字（包括 *Ravelyn, Mistal,
     Jorlan* 这类词典里没有但听上去像真人的名字）。
  2. **可读且不违反英语拼写直觉**——禁用 4 连辅音 / 4 连元音 / 无元音；
     限制长度；过滤不雅子串。
  3. **邮箱 / 显示姓名一致**——`PersonaGenerator.next()` 一次产出
     `(first, last, email_local, password)`，`browser_register` 直接复用
     `mail_provider.last_persona`，避免邮箱姓 `mason` 而注册名 `Emily Johnson`
     这种反欺诈红旗。

主要 API：

    g = PersonaGenerator(catch_all_domain="example.com")
    p = g.next()
    p.first       # "Marielle"
    p.last        # "Hollanton"
    p.email       # "marielle.hollanton94@example.com"
    p.password    # "49notnallohelleiram" (邮箱 local 倒序)
"""
from __future__ import annotations

import random
import re
import string
from dataclasses import dataclass
from typing import Optional

# ───────────────────────── 音节素材 ─────────────────────────

# 起始辅音 / 辅音组（英语合法 onset cluster，全部经过真实 first-name 抽样校准）
_ONSETS_FIRST = [
    "B", "Br", "C", "Ch", "Cl", "Cr", "D", "Dr",
    "F", "Fr", "G", "Gr", "H", "J", "K", "L",
    "M", "N", "P", "Pr", "R", "S", "Sh", "St",
    "T", "Tr", "V", "W", "Z",
    # 元音开头（限于真实存在的：Em-, El-, An-, Al-, Ar-, Ev-, Ad-, Ol-, Is-, Ja-, Ju-, Le-, Li-, Ma-, Mi-, Ro-, Sa-, Ka-）
    "Em", "El", "An", "Al", "Ar", "Ev", "Ad", "Ol",
    "Is", "Ja", "Ju", "Le", "Li", "Ma", "Mi", "Ro", "Sa", "Ka",
    # 给单字母 onset 加权（M / J / S / D / R 是英语 first-name 最常见首字母）
    "M", "M", "J", "J", "S", "S", "D", "R", "L", "C",
]

_ONSETS_LAST = [
    "B", "Bl", "Br", "C", "Ch", "Cl", "Cr", "D",
    "F", "Fr", "G", "Gr", "H", "J", "K", "L",
    "M", "N", "P", "Pr", "R", "S", "Sh", "St",
    "T", "Th", "Tr", "V", "W", "Wr",
    # 高频英语姓首：S / B / H / W / M / J / C / D
    "S", "S", "H", "H", "W", "B", "M", "J", "C", "D",
]

# 元音核（精简到真实 first-name 中常见的，避免 "ya/yo/eo/io/au" 等冷僻组合）
_VOWELS = [
    "a", "a", "a",
    "e", "e", "e",
    "i", "i",
    "o", "o",
    "u",
    "ai", "ay", "ea", "ee", "ie", "oa", "oo", "ou",
]

# 中段辅音（音节连接，去掉 "ck/dd/ff/mm/nn/rr/pp" 等内部双辅音——名字里很少出现）
_MIDDLES = [
    "b", "c", "d", "f", "g", "k", "l", "ll", "m",
    "n", "nd", "nn", "nt", "p", "r", "rd", "rl", "rn",
    "rt", "s", "sh", "ss", "st", "t", "th", "v", "z",
    # 高频内部辅音加权
    "l", "l", "n", "n", "r", "r", "s", "t",
]

# 名字结尾（first name）—— 偏好 -a/-ie/-on/-an/-yn 等真人感强的
_ENDINGS_FIRST = [
    "a", "ah", "an", "ana", "ar", "as", "e", "el",
    "ela", "en", "er", "es", "ette", "ey", "ia",
    "ie", "in", "ina", "is", "ka", "la", "le", "ley",
    "li", "lia", "lie", "ly", "lyn", "ma", "na",
    "ne", "nia", "nna", "o", "on", "or", "ric",
    "rie", "ta", "th", "us", "ya", "yn", "y",
]

# 姓氏结尾—— 英美姓常见后缀
_ENDINGS_LAST = [
    "an", "berg", "by", "den", "der", "don",
    "er", "es", "ett", "field", "ford", "ham",
    "hart", "hill", "house", "ick", "in", "ing",
    "ins", "ism", "land", "ley", "lin", "man",
    "more", "ner", "ney", "or", "ridge", "rne",
    "sen", "son", "ston", "ter", "ton", "vere",
    "way", "well", "wood", "worth", "y",
]

# 常见短姓 / 短名（直接一个音节就完结的概率）
_FIRST_MONO_TAILS = ["", "k", "l", "m", "n", "r", "s", "t", "x", "ck", "th", "rd", "rk", "rt"]
_LAST_MONO_TAILS = ["", "ck", "ld", "ll", "lt", "lts", "n", "nd", "nn", "nt", "nts", "rd", "rs", "rt", "ss", "st", "th"]

# 不雅 / 敏感子串黑名单（生成后扫描，命中即重抽）
_BAD_SUBSTRINGS = (
    "fuck", "shit", "cunt", "dick", "cock", "bitch", "slut",
    "porn", "rape", "nigg", "fag",
    "anal", "anus", "tits", "boob", "pussi",
    "hitler", "nazi",
)


def _is_clean(s: str) -> bool:
    """合法性检查：长度合规 + 含元音 + 不命中黑名单 + 无 4 连相同字符。"""
    low = s.lower()
    if any(b in low for b in _BAD_SUBSTRINGS):
        return False
    if not re.search(r"[aeiouy]", low):
        return False
    if re.search(r"(.)\1{2,}", low):  # 3 个相同字符连排（aaa, lll）
        return False
    if re.search(r"[^aeiouy ]{4,}", low):  # 4 连辅音
        return False
    if re.search(r"[aeiou]{3,}", low):  # 3 连元音（"eei", "ouo", "aae"）
        return False
    # 禁止 2 个连续双元音（如 "iaie", "eaou"）
    diphthongs = re.findall(r"(?:ai|ay|ea|ee|ie|oa|oo|ou|ia|io)", low)
    if len(diphthongs) > 1:
        return False
    return True


# ───────────────────────── 名字合成 ─────────────────────────

def _gen_first_name(rng: random.Random) -> str:
    """生成 first name。1/2/3 音节按权重 0.18 / 0.70 / 0.12。"""
    for _ in range(8):
        syll = rng.choices([1, 2, 3], weights=[18, 70, 12], k=1)[0]
        onset = rng.choice(_ONSETS_FIRST)
        v1 = rng.choice(_VOWELS)
        if syll == 1:
            tail = rng.choice(_FIRST_MONO_TAILS)
            name = onset + v1 + tail
        elif syll == 2:
            mid = rng.choice(_MIDDLES)
            ending = rng.choice(_ENDINGS_FIRST)
            name = onset + v1 + mid + ending
        else:
            mid1 = rng.choice(_MIDDLES)
            v2 = rng.choice(_VOWELS)
            ending = rng.choice(_ENDINGS_FIRST)
            name = onset + v1 + mid1 + v2 + ending
        # 截断 + 长度约束
        if 3 <= len(name) <= 10 and _is_clean(name):
            return name.capitalize()
    # 兜底
    return "Alex"


def _gen_last_name(rng: random.Random) -> str:
    """生成姓。1/2/3 音节按 0.10 / 0.65 / 0.25（姓比名稍长）。"""
    for _ in range(8):
        syll = rng.choices([1, 2, 3], weights=[10, 65, 25], k=1)[0]
        onset = rng.choice(_ONSETS_LAST)
        v1 = rng.choice(_VOWELS)
        if syll == 1:
            tail = rng.choice(_LAST_MONO_TAILS)
            name = onset + v1 + tail
        elif syll == 2:
            mid = rng.choice(_MIDDLES)
            ending = rng.choice(_ENDINGS_LAST)
            name = onset + v1 + mid + ending
        else:
            mid1 = rng.choice(_MIDDLES)
            v2 = rng.choice(_VOWELS)
            ending = rng.choice(_ENDINGS_LAST)
            name = onset + v1 + mid1 + v2 + ending
        if 4 <= len(name) <= 12 and _is_clean(name):
            return name.capitalize()
    return "Walker"


# ───────────────────────── 邮箱前缀模式 ─────────────────────────

_LOCAL_PATTERNS = [
    ("first.last",      14),
    ("firstlast",       10),
    ("first_last",       6),
    ("first.last+num",  18),
    ("firstlast+year",  16),
    ("f.last+num",      14),
    ("firstl+num",      10),
    ("first+year",       8),
    ("first.last.year",  4),
]


def _build_local_part(first: str, last: str, rng: random.Random) -> str:
    """按 first/last 拼出邮箱前缀。保证 [a-z0-9._]，长度 5-22。"""
    f = first.lower()
    l = last.lower()
    pat = rng.choices([p for p, _ in _LOCAL_PATTERNS],
                      weights=[w for _, w in _LOCAL_PATTERNS], k=1)[0]
    if pat == "first.last":
        local = f"{f}.{l}"
    elif pat == "firstlast":
        local = f"{f}{l}"
    elif pat == "first_last":
        local = f"{f}_{l}"
    elif pat == "first.last+num":
        local = f"{f}.{l}{rng.randint(1, 99):02d}"
    elif pat == "firstlast+year":
        local = f"{f}{l}{rng.randint(1985, 2003)}"
    elif pat == "f.last+num":
        local = f"{f[0]}{l}{rng.randint(1, 99):02d}"
    elif pat == "firstl+num":
        local = f"{f}{l[0]}{rng.randint(1, 99):02d}"
    elif pat == "first+year":
        local = f"{f}{rng.randint(1985, 2003)}"
    else:  # first.last.year
        local = f"{f}.{l}.{rng.randint(1985, 2003)}"
    # 截到 22
    if len(local) > 22:
        local = local[:22].rstrip("._")
    # 保证只允许的字符
    local = re.sub(r"[^a-z0-9._]", "", local)
    if len(local) < 5:
        local = (local + f"{rng.randint(100, 999)}")[:8]
    return local


# ───────────────────────── 密码（邮箱 local 倒序） ─────────────────────────

def _password_from_local(local: str) -> str:
    """密码 = 邮箱本地部分倒序；不足 8 字符时用 OpenAI 兜底后缀补齐。

    规则：
      - 倒序后保留所有字符（含 . _ 数字）。OpenAI 接受这些字符。
      - 长度 < 8 → 追加 `@2026Ai`（仍然便于人脑反推：原 local 反读 + 固定尾）
    """
    pwd = local[::-1]
    if len(pwd) < 8:
        pwd = pwd + "@2026Ai"
    return pwd


# ───────────────────────── 对外接口 ─────────────────────────

@dataclass
class Persona:
    first: str           # "Marielle"
    last: str            # "Hollanton"
    email_local: str     # "marielle.hollanton94"
    email: str           # "marielle.hollanton94@example.com"
    password: str        # "49notnalloh.elleiram"

    @property
    def full_name(self) -> str:
        return f"{self.first} {self.last}"


class PersonaGenerator:
    """统一出口：一次产出完整 persona（邮箱+姓名+密码联动）。"""

    def __init__(self, catch_all_domain: str, rng: Optional[random.Random] = None):
        self.catch_all_domain = catch_all_domain
        self._rng = rng or random.Random()

    def next(self) -> Persona:
        first = _gen_first_name(self._rng)
        last = _gen_last_name(self._rng)
        local = _build_local_part(first, last, self._rng)
        email = f"{local}@{self.catch_all_domain}" if self.catch_all_domain else local
        return Persona(
            first=first,
            last=last,
            email_local=local,
            email=email,
            password=_password_from_local(local),
        )
