"""Quote verification: a quote is kept only if it appears verbatim (modulo whitespace, case and
typographic punctuation) in the text we fetched ourselves. Figures must also have their number
inside the quote and their unit nearby."""
from __future__ import annotations

import re
import unicodedata

MIN_QUOTE_CHARS = 20
MAX_QUOTE_CHARS = 600
_TRANS = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2032": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u2033": '"',
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2212": "-",
    "\u00a0": " ", "\u202f": " ", "\u2009": " ", "\u00ad": "",
})
_NUM = re.compile(r"(?<![\w.])(\d{1,3}(?:[,\s]\d{3})+|\d+)(?:\.(\d+))?")
_SCALE = {"thousand": 1e3, "million": 1e6, "mn": 1e6, "billion": 1e9, "bn": 1e9, "trillion": 1e12}
UNIT_SYNONYMS = {
    "MWh": ["mwh", "megawatt hour", "megawatt-hour"], "GWh": ["gwh", "gigawatt hour", "gigawatt-hour"],
    "TWh": ["twh", "terawatt hour", "terawatt-hour"], "kWh": ["kwh", "kilowatt hour", "kilowatt-hour"],
    "PWh": ["pwh"], "Wh": ["wh"],
    "GJ": ["gj", "gigajoule"], "TJ": ["tj", "terajoule"], "PJ": ["pj", "petajoule"], "MJ": ["mj", "megajoule"],
    "EJ": ["ej", "exajoule"], "kJ": ["kj"], "J": ["joule"], "MMBtu": ["mmbtu", "million btu"],
    "MW": ["mw", "megawatt"], "GW": ["gw", "gigawatt"], "kW": ["kw", "kilowatt"], "TW": ["tw", "terawatt"],
    "W": ["watt"],
    "USD": ["$", "usd", "dollar"], "USD_thousands": ["$", "usd", "thousand", "dollar"],
    "USD_millions": ["$", "usd", "million", "dollar"], "USD_billions": ["$", "usd", "billion", "dollar"],
}


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "").translate(_TRANS)
    return re.sub(r"\s+", " ", t).strip().lower()


def find_quote(quote: str, source_text_norm: str) -> int:
    """Index of the normalized quote in the normalized source text, or -1."""
    q = normalize(quote).strip(" \"'")
    if len(q) < MIN_QUOTE_CHARS or len(q) > MAX_QUOTE_CHARS:
        return -1
    return source_text_norm.find(q)


def context_around(source_text_norm: str, idx: int, quote: str, pad: int = 300) -> str:
    q = normalize(quote).strip(" \"'")
    return source_text_norm[max(0, idx - pad): idx + len(q) + pad]


def numbers_in(text: str) -> list[float]:
    out: list[float] = []
    t = normalize(text)
    for m in _NUM.finditer(t):
        whole = re.sub(r"[,\s]", "", m.group(1))
        n = float(f"{whole}.{m.group(2)}") if m.group(2) else float(whole)
        out.append(n)
        tail = t[m.end(): m.end() + 12].strip()
        for word, mult in _SCALE.items():
            if tail.startswith(word):
                out.append(n * mult)
    return out


def number_matches(value: float, quote: str, tol: float = 0.005) -> bool:
    for n in numbers_in(quote):
        if value == n or (n and abs(value - n) / abs(n) <= tol):
            return True
    return False


def unit_present(unit: str, text: str) -> bool:
    t = normalize(text)
    for syn in UNIT_SYNONYMS.get(unit, [unit.lower()]):
        if syn in ("$",):
            if "$" in t:
                return True
            continue
        if re.search(rf"(?<![a-z]){re.escape(syn)}", t):
            return True
    return False


KEYWORDS = re.compile(
    r"(mwh|gwh|twh|kwh|gigajoule|terajoule|petajoule|\bgj\b|\btj\b|\bpj\b|mmbtu|energy consumption|"
    r"electricity|data cent(?:er|re)s?|megawatt|gigawatt|\bmw\b|\bgw\b|capital expenditure|capex|"
    r"launch|shipped|first|record|open[- ]source|regulat|permit|policy|lobby|advoca|breakthrough)",
    re.IGNORECASE)


def windows(text: str, budget: int, pad: int = 600) -> str:
    """Pick keyword-dense windows from a long document so extraction stays within budget.
    Windows are verbatim slices, so quotes copied from them can be verified against the full text."""
    if len(text) <= budget:
        return text
    spans: list[list[int]] = []
    for m in KEYWORDS.finditer(text):
        s, e = max(0, m.start() - pad), min(len(text), m.end() + pad)
        if spans and s <= spans[-1][1]:
            spans[-1][1] = e
        else:
            spans.append([s, e])
    if not spans:
        return text[:budget]
    # densest first, then restore document order
    scored = sorted(spans, key=lambda sp: -len(KEYWORDS.findall(text, sp[0], sp[1])) / (sp[1] - sp[0]))
    chosen, used = [], 0
    for s, e in scored:
        if used >= budget:
            break
        e = min(e, s + (budget - used))
        chosen.append((s, e))
        used += e - s
    return " … ".join(text[s:e] for s, e in sorted(chosen))
