"""Identify which product a listing is actually selling.

Marketplace titles are written by hand and a search for one model returns many
others: a query for an iPad Air 5 also surfaces older Airs, base iPads, Pros and
loose accessories. Averaging those together produces a number that describes
nothing, so each listing is classified and the statistics are grouped by model.

Classification is deliberately conservative. A title that does not clearly name
a model becomes UNKNOWN rather than being guessed into a bucket, because a wrong
label corrupts an average silently while an honest UNKNOWN is visible.
"""

import re
import unicodedata
from typing import List, Optional, Tuple

UNKNOWN = "unknown"
ACCESSORY = "accessory"
OTHER = "other-product"
FOR_PARTS = "for-parts"


def normalize(text: str) -> str:
    """Lowercase, strip accents and collapse whitespace, for robust matching."""
    stripped = "".join(
        char
        for char in unicodedata.normalize("NFD", text.lower())
        if unicodedata.category(char) != "Mn"
    )
    # Ordinals appear as "4a", "4.a", "4ª" and "4.ª". Drop the dot before the
    # ordinal mark first, otherwise removing the mark strands it as "4." and the
    # generation no longer reads as a token.
    stripped = re.sub(r"\.\s*(?=[ºª])", "", stripped)
    stripped = stripped.replace("º", "").replace("ª", "")
    stripped = re.sub(r"(\d)\.(?=[a-z])", r"\1", stripped)
    return re.sub(r"\s+", " ", stripped).strip()


# Spanish and English ordinals share these listings, often abbreviated.
def _gen(number: int, *words: str) -> str:
    """A pattern matching '5', '5ta', 'quinta', '5th' ... near 'gen'."""
    forms = [str(number), f"{number}a", f"{number}ta", f"{number}da", f"{number}ra", f"{number}na",
             f"{number}ma", f"{number}th", f"{number}nd", f"{number}rd", f"{number}st", *words]
    return r"(?:" + "|".join(re.escape(f) for f in forms) + r")"


def _generation(text: str, number: int, *words: str) -> bool:
    """True when the text names this generation, as a digit, ordinal or word."""
    token = _gen(number, *words)
    return bool(
        re.search(rf"\b{token}\s*(?:a\s*)?gen(?:eracion|eration)?\b", text)
        or re.search(rf"\bgen(?:eracion|eration)?\s*{token}\b", text)
    )


def _air_generation(text: str, number: int, *words: str) -> bool:
    """True for 'Air 4', 'Air 4ta', 'Air 5th' -- a model named without the word
    'generacion', which sellers frequently omit."""
    return bool(re.search(rf"\bair\s*{_gen(number, *words)}\b", text))


# Ordered rules: the first match wins, so the most specific come first.
# Each entry is (label, predicate).
_RULES: List[Tuple[str, object]] = []


def _rule(label: str):
    def register(fn):
        _RULES.append((label, fn))
        return fn

    return register


@_rule(FOR_PARTS)
def _is_for_parts(t: str) -> bool:
    """A unit sold for parts is not a working device and must not set a price."""
    return bool(
        re.search(r"\b(para\s+(repuesto|piezas|reparar|desarme)|repuesto|no\s+(funciona|"
                  r"enciende|carga)|para\s+reparacion|dañad|roto)\b", t)
    )


@_rule(ACCESSORY)
def _is_accessory(t: str) -> bool:
    # An accessory listing may still name a model, so this must be tested first.
    accessory = (
        r"\b(lapiz|pencil|magic keyboard|teclado|funda|case|carcasa|mica|"
        r"cargador|cable|soporte|protector|pantalla|bateria)\b"
    )
    if not re.search(accessory, t):
        return False
    # "iPad Air 5 + Apple Pencil" is a tablet sold with an extra, not an
    # accessory listing: a leading device word means the device is the product.
    return not re.search(r"^\W*(vendo\s+)?i?pad\b", t)


@_rule(OTHER)
def _is_other_product(t: str) -> bool:
    return bool(re.search(r"\b(macbook|iphone|imac|airpods|watch|samsung|galaxy|tab)\b", t))


@_rule("iPad Pro")
def _is_pro(t: str) -> bool:
    return "ipad pro" in t or bool(re.search(r"\bpad pro\b", t))


@_rule("iPad Air M4")
def _is_air_m4(t: str) -> bool:
    return _is_air(t) and bool(re.search(r"\bm4\b", t))


@_rule("iPad Air M2 (6th)")
def _is_air_m2(t: str) -> bool:
    return _is_air(t) and (bool(re.search(r"\bm2\b", t)) or _generation(t, 6, "sexta"))


@_rule("iPad Air 5 (M1)")
def _is_air_5(t: str) -> bool:
    if not _is_air(t):
        return False
    return (
        bool(re.search(r"\bm1\b", t))
        or _generation(t, 5, "quinta")
        or _air_generation(t, 5, "quinta")
    )


@_rule("iPad Air 4")
def _is_air_4(t: str) -> bool:
    return _is_air(t) and (_generation(t, 4, "cuarta") or _air_generation(t, 4, "cuarta"))


@_rule("iPad Air 3")
def _is_air_3(t: str) -> bool:
    return _is_air(t) and (_generation(t, 3, "tercera") or _air_generation(t, 3, "tercera"))


@_rule("iPad Air 2")
def _is_air_2(t: str) -> bool:
    return _is_air(t) and _air_generation(t, 2, "segunda")


@_rule("iPad Air 1")
def _is_air_1(t: str) -> bool:
    return _is_air(t) and (
        _generation(t, 1, "primera") or "2013" in t or "2014" in t
    )


@_rule("iPad (base)")
def _is_base_ipad(t: str) -> bool:
    # A generation with no "air" or "pro" qualifier is a base iPad.
    if _is_air(t) or "pro" in t or "mini" in t:
        return False
    return any(
        _generation(t, number, word)
        for number, word in (
            (3, "tercera"), (5, "quinta"), (6, "sexta"), (7, "septima"), (8, "octava"),
            (9, "novena"), (10, "decima"),
        )
    )


def _is_air(text: str) -> bool:
    return bool(re.search(r"\bair\b", text))


def classify(title: str, description: str | None = None) -> str:
    """Return a model label for a listing, or UNKNOWN when it is not clear.

    The description is consulted only to break ties the title cannot, since
    descriptions often mention other models the seller also owns or compares to.
    """
    for source in (title, f"{title} {description or ''}"):
        text = normalize(source)
        if not text:
            continue
        for label, predicate in _RULES:
            if predicate(text):
                return label
        if not description:
            break
    return UNKNOWN


def is_product(label: str) -> bool:
    """True when the label names a working device rather than parts or noise."""
    return label not in (UNKNOWN, ACCESSORY, OTHER, FOR_PARTS)


# A bundled Apple Pencil is only worth the 2nd-generation price; a 1st gen sells
# for materially less. Sellers state the generation inconsistently, and the
# generation words also appear for the tablet itself ("iPad Air 5ta generación
# + apple pencil"), so match only a generation attached to the pencil.
_PENCIL_GENERATION = (
    (2, re.compile(r"(?:pencil|lapiz)\s*(?:de\s*)?(?:2|ii|2a|2da|2nd|segunda|pro)\b")),
    (2, re.compile(r"\b(?:2|2a|2da|segunda)\s*(?:a\s*)?gen\w*\s*(?:de\s*)?(?:pencil|lapiz)")),
    (1, re.compile(r"(?:pencil|lapiz)\s*(?:de\s*)?(?:1|1a|1ra|1era|primera|1st)\b")),
    (1, re.compile(r"\b(?:1|1a|1ra|primera)\s*(?:a\s*)?gen\w*\s*(?:de\s*)?(?:pencil|lapiz)")),
)

_HAS_PENCIL = re.compile(r"\b(?:apple\s*)?(?:pencil|lapiz)\b")


def pencil(title: str, description: str | None = None) -> Optional[int]:
    """Which Apple Pencil generation a listing includes.

    Returns 2 or 1 when the listing says so, 0 when a pencil is mentioned but
    the generation is not stated, and None when no pencil is offered at all.
    The distinction matters: only a stated 2nd generation is worth the 2nd
    generation resale price, and guessing costs real money either way.
    """
    text = normalize(f"{title} {description or ''}")
    if not _HAS_PENCIL.search(text):
        return None
    for generation, pattern in _PENCIL_GENERATION:
        if pattern.search(text):
            return generation
    return 0
