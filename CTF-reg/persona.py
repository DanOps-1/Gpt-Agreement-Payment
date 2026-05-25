"""Algorithmic name generation + email prefix generator (syllable synthesis method).

Design goals:
  1. **No dependency on fixed name tables** — randomly assemble through syllables (onset+vowel+coda+ending) following English spelling rules, theoretically generating 10,000+ new names (including names like *Ravelyn, Mistal, Jorlan* that don't exist in dictionaries but sound like real people).
  2. **Readable and doesn't violate English spelling intuition** — prohibit 4 consecutive consonants / 4 consecutive vowels / no vowels; limit length; filter indecent substrings.
  3. **Email / display name consistency** — `PersonaGenerator.next()` outputs `(first, last, email_local, password)` once, `browser_register` directly reuses `mail_provider.last_persona`, avoiding anti-fraud red flags like email surname `mason` but registration name `Emily Johnson`.

Main API:

    g = PersonaGenerator(catch_all_domain="example.com")
    p = g.next()
    p.first       # "Marielle"
    p.last        # "Hollanton"
    p.email       # "marielle.hollanton94@example.com"
    p.password    # "49notnallohelleiram" (email local reversed)"""
from __future__ import annotations

import random
import re
import string
from dataclasses import dataclass
from typing import Optional

# ───────────────────────── Syllable materials ─────────────────────────

# Initial consonants / consonant clusters (legal English onset clusters, all calibrated by real first-name sampling)
_ONSETS_FIRST = [
    "B", "Br", "C", "Ch", "Cl", "Cr", "D", "Dr",
    "F", "Fr", "G", "Gr", "H", "J", "K", "L",
    "M", "N", "P", "Pr", "R", "S", "Sh", "St",
    "T", "Tr", "V", "W", "Z",
    # Vowel-initial names (limited to actually existing: Em-, El-, An-, Al-, Ar-, Ev-, Ad-, Ol-, Is-, Ja-, Ju-, Le-, Li-, Ma-, Mi-, Ro-, Sa-, Ka-)
    "Em", "El", "An", "Al", "Ar", "Ev", "Ad", "Ol",
    "Is", "Ja", "Ju", "Le", "Li", "Ma", "Mi", "Ro", "Sa", "Ka",
    # Weight single-letter onsets (M / J / S / D / R are the most common first letters in English first-names)
    "M", "M", "J", "J", "S", "S", "D", "R", "L", "C",
]

_ONSETS_LAST = [
    "B", "Bl", "Br", "C", "Ch", "Cl", "Cr", "D",
    "F", "Fr", "G", "Gr", "H", "J", "K", "L",
    "M", "N", "P", "Pr", "R", "S", "Sh", "St",
    "T", "Th", "Tr", "V", "W", "Wr",
    # High-frequency English surname initials: S / B / H / W / M / J / C / D
    "S", "S", "H", "H", "W", "B", "M", "J", "C", "D",
]

# Vowel nucleus (simplified to those commonly found in real first-names, avoid obscure combinations like "ya/yo/eo/io/au")
_VOWELS = [
    "a", "a", "a",
    "e", "e", "e",
    "i", "i",
    "o", "o",
    "u",
    "ai", "ay", "ea", "ee", "ie", "oa", "oo", "ou",
]

# Mid-syllable consonants (for syllable connection, remove internal double consonants like "ck/dd/ff/mm/nn/rr/pp" — rarely appear in names)
_MIDDLES = [
    "b", "c", "d", "f", "g", "k", "l", "ll", "m",
    "n", "nd", "nn", "nt", "p", "r", "rd", "rl", "rn",
    "rt", "s", "sh", "ss", "st", "t", "th", "v", "z",
    # High-frequency internal consonants with weighting
    "l", "l", "n", "n", "r", "r", "s", "t",
]

# Name endings (first name) — favor -a/-ie/-on/-an/-yn etc. that feel authentic
_ENDINGS_FIRST = [
    "a", "ah", "an", "ana", "ar", "as", "e", "el",
    "ela", "en", "er", "es", "ette", "ey", "ia",
    "ie", "in", "ina", "is", "ka", "la", "le", "ley",
    "li", "lia", "lie", "ly", "lyn", "ma", "na",
    "ne", "nia", "nna", "o", "on", "or", "ric",
    "rie", "ta", "th", "us", "ya", "yn", "y",
]

# Surname endings — common English/American surname suffixes
_ENDINGS_LAST = [
    "an", "berg", "by", "den", "der", "don",
    "er", "es", "ett", "field", "ford", "ham",
    "hart", "hill", "house", "ick", "in", "ing",
    "ins", "ism", "land", "ley", "lin", "man",
    "more", "ner", "ney", "or", "ridge", "rne",
    "sen", "son", "ston", "ter", "ton", "vere",
    "way", "well", "wood", "worth", "y",
]

# Common short surnames / short names (probability of completing in just one syllable)
_FIRST_MONO_TAILS = ["", "k", "l", "m", "n", "r", "s", "t", "x", "ck", "th", "rd", "rk", "rt"]
_LAST_MONO_TAILS = ["", "ck", "ld", "ll", "lt", "lts", "n", "nd", "nn", "nt", "nts", "rd", "rs", "rt", "ss", "st", "th"]

# Indecent / sensitive substring blacklist (scan after generation, resample if hit)
_BAD_SUBSTRINGS = (
    "fuck", "shit", "cunt", "dick", "cock", "bitch", "slut",
    "porn", "rape", "nigg", "fag",
    "anal", "anus", "tits", "boob", "pussi",
    "hitler", "nazi",
)


def _is_clean(s: str) -> bool:
    """Legality check: length compliant + contains vowels + no blacklist hits + no 4 consecutive identical characters."""
    low = s.lower()
    if any(b in low for b in _BAD_SUBSTRINGS):
        return False
    if not re.search(r"[aeiouy]", low):
        return False
    if re.search(r"(.)\1{2,}", low):  # 3 identical consecutive characters (aaa, lll)
        return False
    if re.search(r"[^aeiouy ]{4,}", low):  # 4 consecutive consonants
        return False
    if re.search(r"[aeiou]{3,}", low):  # 3 consecutive vowels ("eei", "ouo", "aae")
        return False
    # Prohibit 2 consecutive double vowels (like "iaie", "eaou")
    diphthongs = re.findall(r"(?:ai|ay|ea|ee|ie|oa|oo|ou|ia|io)", low)
    if len(diphthongs) > 1:
        return False
    return True


# ───────────────────────── Name synthesis ─────────────────────────

def _gen_first_name(rng: random.Random) -> str:
    """Generate first name. 1/2/3 syllables weighted 0.18 / 0.70 / 0.12."""
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
        # Truncate + apply length constraints
        if 3 <= len(name) <= 10 and _is_clean(name):
            return name.capitalize()
    # Fallback
    return "Alex"


def _gen_last_name(rng: random.Random) -> str:
    """Generate surname. 1/2/3 syllables weighted 0.10 / 0.65 / 0.25 (surnames slightly longer than first names)."""
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


# ───────────────────────── Email prefix pattern ─────────────────────────

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
    """Spell out email prefix from first/last. Ensure [a-z0-9._], length 5-22."""
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
    # Truncate to 22
    if len(local) > 22:
        local = local[:22].rstrip("._")
    # Ensure only allowed characters
    local = re.sub(r"[^a-z0-9._]", "", local)
    if len(local) < 5:
        local = (local + f"{rng.randint(100, 999)}")[:8]
    return local


# ───────────────────────── Password (email local reversed) ─────────────────────────

def _password_from_local(local: str) -> str:
    """Password = email local part reversed; if less than 8 characters, use OpenAI fallback suffix to pad.

    Rules:
      - Keep all characters after reversal (including . _ digits). OpenAI accepts these characters.
      - Length < 8 → append `@2026Ai` (still easy for human brain to reverse-engineer: original local read backwards + fixed suffix)"""
    pwd = local[::-1]
    if len(pwd) < 8:
        pwd = pwd + "@2026Ai"
    return pwd


# ───────────────────────── Public interface ─────────────────────────

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
    """Unified exit: output complete persona once (email + name + password linked)."""

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
