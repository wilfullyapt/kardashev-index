"""Quote verification and extraction windows.

A quote is kept only if it is present in the text we fetched ourselves. Matching tolerates what
PDF/HTML text extraction and a model's copy typically differ by — whitespace and line breaks,
case, typographic punctuation, Unicode compatibility forms (NBSP, en-space, ligatures),
hyphenation split across lines ("trillion- parameter"), words glued or split by the extractor,
footnote markers ("emissions.1 NVIDIA") and an elided middle ("… ") — but never different words or
digits: both sides are reduced to the same sequence of letters and digits and that sequence must
occur in the source. The quote we store is the matching span copied from the source itself.
Figures must also have their value in that source span and their unit nearby."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

MIN_QUOTE_CHARS = 20
MAX_QUOTE_CHARS = 600
MIN_SKELETON_CHARS = 14      # letters+digits a quote must contain to be meaningful
MAX_ELLIPSIS_GAP = 400       # skeleton chars allowed between the pieces of an elided quote
_TRANS = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2032": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u2033": '"',
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2212": "-",
    "\u00a0": " ", "\u202f": " ", "\u2009": " ", "\u2002": " ", "\u2003": " ", "\u2007": " ",
    "\u00ad": "", "\u200b": "", "\u2060": "", "\ufeff": "",
})
_SCALE = {"thousand": 1e3, "million": 1e6, "mn": 1e6, "billion": 1e9, "bn": 1e9, "trillion": 1e12}
UNIT_SYNONYMS = {
    "MWh": ["mwh", "megawatt hour", "megawatt-hour", "megawatthour"],
    "GWh": ["gwh", "gigawatt hour", "gigawatt-hour", "gigawatthour"],
    "TWh": ["twh", "terawatt hour", "terawatt-hour", "terawatthour"],
    "kWh": ["kwh", "kilowatt hour", "kilowatt-hour", "kilowatthour"],
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


def squash(text: str) -> str:
    """Whitespace-collapsed copy of source text (original case and characters kept)."""
    return re.sub(r"\s+", " ", (text or "").replace("\u00ad", "")).strip()


# ---------------- skeleton matching ----------------

def _keep(ch: str) -> str:
    """Letters/digits a character contributes to the skeleton ('' for punctuation, symbols, space)."""
    if not ch.isalnum():
        return ""
    if unicodedata.category(ch)[0] not in "LN":
        return ""
    t = unicodedata.normalize("NFKC", ch).lower()
    return "".join(c for c in t if c.isalnum())


def _skeleton(text: str, drop_footnotes: bool = False) -> tuple[str, list[int]]:
    out: list[str] = []
    pos: list[int] = []
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if drop_footnotes and ch.isdigit() and i > 0:
            j = i
            while j < n and text[j].isdigit():
                j += 1
            prev = text[i - 1]
            prev2 = text[i - 2] if i > 1 else " "
            after = text[j] if j < n else " "
            marker = (j - i <= 2 and (after.isspace() or after in ".,;:)") and
                      (prev.islower() or (prev in ".,;:)\u201d\"'" and (prev2.isalpha() or prev2 == ")"))))
            if marker:
                i = j
                continue
            for k in range(i, j):
                for c in _keep(text[k]):
                    out.append(c)
                    pos.append(k)
            i = j
            continue
        for c in _keep(ch):
            out.append(c)
            pos.append(i)
        i += 1
    return "".join(out), pos


def skeleton(text: str) -> str:
    return _skeleton(text)[0]


@dataclass
class Match:
    start: int        # span in the original source text
    end: int
    method: str       # exact | loose | footnote | ellipsis
    text: str         # the source span, whitespace-collapsed — this is what we store and show


@dataclass
class SourceText:
    text: str
    norm: str = ""
    _sk: dict = field(default_factory=dict, repr=False)

    def __post_init__(self):
        self.norm = normalize(self.text)

    def sk(self, drop_footnotes: bool = False) -> tuple[str, list[int]]:
        if drop_footnotes not in self._sk:
            self._sk[drop_footnotes] = _skeleton(self.text, drop_footnotes)
        return self._sk[drop_footnotes]

    def span(self, start: int, end: int, method: str) -> Match:
        return Match(start, end, method, squash(self.text[start:end]))

    def context(self, m: Match, pad: int = 300) -> str:
        return squash(self.text[max(0, m.start - pad): m.end + pad])


_ELLIPSIS = re.compile(r"\s*(?:\[\s*(?:\.\s*){3}\]|\[\s*…\s*\]|\.\s*\.\s*\.|…)\s*")


def _edge(s: str) -> int:
    """Number of non-space punctuation/symbol characters at the start of s ('$', '(', '"' …)."""
    n = 0
    for ch in s:
        if ch.isspace() or _keep(ch):
            break
        n += 1
    return n


def _widen(st: SourceText, a: int, b: int, quote: str) -> tuple[int, int]:
    """Include leading/trailing symbols the quote has ('$4.2 billion', '100%', 'generation.')."""
    t = st.text
    lead, trail = _edge(quote.strip()), _edge(quote.strip()[::-1])
    while lead and a > 0 and not t[a - 1].isspace() and not _keep(t[a - 1]):
        a, lead = a - 1, lead - 1
    while trail and b < len(t) and not t[b].isspace() and not _keep(t[b]):
        b, trail = b + 1, trail - 1
    return a, b


def _find_sk(q: str, st: SourceText, method: str, quote: str, drop: bool = False) -> Match | None:
    sk, pos = st.sk(drop)
    i = sk.find(q)
    if i < 0:
        return None
    a, b = _widen(st, pos[i], pos[i + len(q) - 1] + 1, quote)
    return st.span(a, b, method)


def locate(quote: str, st: SourceText) -> Match | None:
    """Find the model's quote in the fetched source text, or None (see module docstring)."""
    qn = normalize(quote).strip(" \"'")
    if len(qn) < MIN_QUOTE_CHARS or len(qn) > MAX_QUOTE_CHARS:
        return None
    pieces = [p for p in _ELLIPSIS.split(quote) if skeleton(p)]
    if len(pieces) > 1:
        return _locate_elided(pieces, st)
    q = skeleton(quote)
    if len(q) < MIN_SKELETON_CHARS:
        return None
    method = "exact" if qn in st.norm else "loose"
    return _find_sk(q, st, method, quote) or _find_sk(q, st, "footnote", quote, drop=True)


def _locate_elided(pieces: list[str], st: SourceText) -> Match | None:
    sks = [skeleton(p) for p in pieces]
    if sum(map(len, sks)) < MIN_SKELETON_CHARS or any(len(s) < 6 for s in sks):
        return None
    sk, pos = st.sk(False)
    start = sk.find(sks[0])
    while start >= 0:
        cur = start + len(sks[0])
        ok = True
        for s in sks[1:]:
            j = sk.find(s, cur)
            if j < 0 or j - cur > MAX_ELLIPSIS_GAP:
                ok = False
                break
            cur = j + len(s)
        if ok:
            a, b = _widen(st, pos[start], pos[cur - 1] + 1, pieces[0] + " " + pieces[-1])
            if b - a > MAX_QUOTE_CHARS * 2:
                return None
            return st.span(a, b, "ellipsis")
        start = sk.find(sks[0], start + 1)
    return None


def find_quote(quote: str, source_text_norm: str) -> int:
    """Index of the quote in normalized source text, or -1 (tolerant matching, see locate)."""
    m = locate(quote, SourceText(source_text_norm))
    return m.start if m else -1


def context_around(source_text_norm: str, idx: int, quote: str, pad: int = 300) -> str:
    q = normalize(quote).strip(" \"'")
    return source_text_norm[max(0, idx - pad): idx + len(q) + pad]


# ---------------- numbers & units ----------------

_COMMA = re.compile(r"(?<![\d.])(\d{1,3}(?:,\d{3})+)(\.\d+)?")
_SPACED = re.compile(r"(?<![\d.,])(\d{1,3}(?: \d{3})+)(\.\d+)?(?![\d,])")
_DOTTED = re.compile(r"(?<![\d,.])(\d{1,3}(?:\.\d{3}){2,})(?:,(\d+))?(?![\d.])")
_PLAIN = re.compile(r"(?<![\d,])(\d+)(\.\d+)?(?![\d]|,\d)")


def numbers_in(text: str) -> list[float]:
    """Every number the text can be read as. Table rows ("1,053,479 815,864") yield each cell;
    space-grouped thousands ("1 053 479") and dotted thousands ("1.053.479") are also read whole;
    a following scale word ("4.2 billion") adds the scaled value."""
    t = normalize(text)
    found: list[tuple[int, float]] = []
    for m in _COMMA.finditer(t):
        found.append((m.end(), float(m.group(1).replace(",", "") + (m.group(2) or ""))))
    for m in _SPACED.finditer(t):
        found.append((m.end(), float(m.group(1).replace(" ", "") + (m.group(2) or ""))))
    for m in _DOTTED.finditer(t):
        found.append((m.end(), float(m.group(1).replace(".", "") + (f".{m.group(2)}" if m.group(2) else ""))))
    for m in _PLAIN.finditer(t):
        found.append((m.end(), float(m.group(1) + (m.group(2) or ""))))
    out: list[float] = []
    for end, n in found:
        out.append(n)
        tail = t[end: end + 12].strip()
        for word, mult in _SCALE.items():
            if re.match(rf"{word}\b", tail):
                out.append(n * mult)
    return out


def number_matches(value: float, quote: str, tol: float = 0.005) -> bool:
    for n in numbers_in(quote):
        if value == n or (n and abs(value - n) / abs(n) <= tol):
            return True
    return False


def unit_present(unit: str, text: str, raw_unit: str | None = None) -> bool:
    """The canonical unit (or the unit exactly as the model wrote it) appears in the text."""
    t = normalize(text)
    syns = list(UNIT_SYNONYMS.get(unit, [unit.lower()]))
    for syn in syns:
        if syn in ("$",):
            if "$" in t:
                return True
            continue
        if re.search(rf"(?<![a-z]){re.escape(syn)}", t):
            return True
    if raw_unit:
        words = [w for w in re.split(r"[\s/()]+", normalize(raw_unit)) if w and w not in ("per", "year", "yr", "a")]
        squashed = t.replace("-", " ")
        if words and all(re.search(rf"(?<![a-z]){re.escape(w.rstrip('s'))}", squashed) for w in words):
            return True
    return False


# ---------------- extraction windows ----------------

GAP = "\n[…]\n"
_QUANT = re.compile(
    r"(\b[mgtkp]wh\b|megawatt[- ]?hours?|gigawatt[- ]?hours?|terawatt[- ]?hours?|kilowatt[- ]?hours?|"
    r"gigajoules?|terajoules?|petajoules?|\b[gtp]j\b|mmbtu|\b[mg]w\b|megawatts?|gigawatts?|"
    r"energy (?:consumption|consumed|use|usage)|electricity (?:consumption|consumed|use|usage|purchased)|"
    r"total energy|fuel consumption|gri 302|it load|data ?cent(?:er|re)s? capacity|"
    r"capital expenditures?|\bcapex\b|purchases? of property)", re.IGNORECASE)
_UNIT_NUM = re.compile(r"\d[\d,.]*\s?(?:[mgtk]wh|[gtp]j|mmbtu|[mg]w)\b|\((?:[mgtk]wh|[gtp]j|mmbtu)\)", re.IGNORECASE)
_BIGNUM = re.compile(r"\b\d{1,3}(?:,\d{3})+\b")
_QUAL = re.compile(
    r"(launch\w*|ship(?:s|ped|ping)\b|introduc(?:ed|es|ing)\b|announc\w+|unveil\w*|breakthrough|first\b|record\b|"
    r"faster|x (?:faster|lower|more)|\d+x\b|open[- ]source|permit\w*|polic(?:y|ies)|regulat\w+|advoca\w+|"
    r"lobb\w+|legislat\w+|grid\b|nuclear|power plant|build(?:s|ing)? out|construct\w*|came online|online\b|"
    r"gigawatt-scale|ai factor(?:y|ies)|supercomputer|data cent(?:er|re)s?)", re.IGNORECASE)
_BOILER = re.compile(
    r"(forward-looking statements?|safe harbor|cookie|privacy (?:policy|statement)|all rights reserved|"
    r"terms of (?:use|service)|subscribe|sign up|newsletter|share (?:on )?(?:twitter|linkedin|facebook)|"
    r"press contacts?|media contacts?|securities and exchange commission|risk factors|"
    r"no obligation to update)", re.IGNORECASE)
CHUNK = 1100


def _chunks(text: str, size: int = CHUNK) -> list[tuple[int, int]]:
    """Consecutive spans of ~size chars, cut at whitespace so words and numbers stay whole."""
    spans, s, n = [], 0, len(text)
    while s < n:
        e = min(n, s + size)
        if e < n:
            cut = text.rfind(" ", s + size // 2, e)
            e = cut if cut > s else e
        spans.append((s, e))
        s = e
        while s < n and text[s] == " ":
            s += 1
    return spans


def score_chunk(chunk: str) -> tuple[float, float]:
    """(quantitative score, qualitative score) for one chunk; boilerplate counts against both."""
    quant = len(_QUANT.findall(chunk))
    unit_nums = len(_UNIT_NUM.findall(chunk))
    big = len(_BIGNUM.findall(chunk)) if (quant or unit_nums) else 0
    qual = min(len({m.lower() for m in _QUAL.findall(chunk)}), 10)   # distinct: running headers repeat
    boiler = min(4.0 * len(_BOILER.findall(chunk)), 12.0)
    return 4.0 * quant + 6.0 * unit_nums + min(big, 12) * 0.75 - boiler, 1.0 * qual - boiler


@dataclass
class Plan:
    spans: list[tuple[int, int]]
    quant: list[float]
    qual: list[float]

    @property
    def scores(self) -> list[float]:
        return [a + b for a, b in zip(self.quant, self.qual, strict=True)]

    @property
    def relevance(self) -> float:
        top = sorted((x for x in self.quant if x > 0), reverse=True)[:8]
        topq = sorted((x for x in self.qual if x > 0), reverse=True)[:8]
        return sum(top) + 2.0 * sum(topq)


def plan(text: str, size: int = CHUNK) -> Plan:
    spans = _chunks(text, size)
    sc = [score_chunk(text[s:e]) for s, e in spans]
    return Plan(spans, [a for a, _ in sc], [b for _, b in sc])


QUANT_SHARE = 0.6   # of a source's budget for figure-bearing chunks; the rest for qualitative evidence


def windows(text: str, budget: int, p: Plan | None = None) -> str:
    """Pick the most relevant ~1 kB chunks of a long document so extraction stays within budget.
    About 60% goes to energy/capacity tables and figures (with the chunk after each strong hit,
    since tables run on), the rest to the most qualitative chunks (launches, build-outs, policy);
    boilerplate ranks last. Chunks are verbatim slices, in document order, separated by GAP, so
    quotes copied from them can be verified against the full text."""
    if len(text) <= budget:
        return text
    p = p or plan(text, min(CHUNK, max(300, budget // 2)))
    chosen: set[int] = set()
    used = 0

    def take(i: int, cap: int) -> bool:
        nonlocal used
        if i in chosen or not 0 <= i < len(p.spans):
            return False
        s, e = p.spans[i]
        if used + (e - s) > cap:
            return False
        chosen.add(i)
        used += e - s
        return True

    def fill(scores: list[float], cap: int, follow: float | None = None):
        for i in sorted(range(len(scores)), key=lambda i: (-scores[i], i)):
            if scores[i] <= 0 or used >= cap:
                break
            if take(i, cap) and follow is not None and scores[i] >= follow:
                take(i + 1, cap)

    if budget >= 4 * CHUNK:
        take(0, budget)                            # title / lede: who is speaking, which year
    fill(p.quant, int(budget * QUANT_SHARE), follow=10)
    fill(p.qual, budget)
    fill(p.scores, budget)
    if used < budget // 2:                         # little signal: fall back to the start of the document
        for i in range(len(p.spans)):
            take(i, budget)
    out, prev_end = [], None
    for i in sorted(chosen):
        s, e = p.spans[i]
        if prev_end is not None and i - 1 in chosen:
            out[-1] = out[-1] + text[prev_end:e]
        else:
            out.append(text[s:e])
        prev_end = e
    return GAP.join(out)


def allocate(plans: dict, lengths: dict, total: int, per_cap: int, floor: int = 2500) -> dict:
    """Split the extraction budget across sources: every source gets a floor, the rest goes to the
    sources with the most relevant content (energy tables first), capped per source."""
    budgets = {k: min(lengths[k], floor) for k in plans}
    left = max(0, total - sum(budgets.values()))
    rel = {k: min(plans[k].relevance, 300.0) ** 0.5 for k in plans}
    while left > 0:
        open_ = {k: r for k, r in rel.items() if budgets[k] < min(lengths[k], per_cap)}
        if not open_:
            break
        weight = sum(open_.values()) or float(len(open_))
        given = 0
        for k, r in open_.items():
            share = int(left * ((r or (1.0 if not sum(open_.values()) else 0)) / weight))
            room = min(lengths[k], per_cap) - budgets[k]
            add = max(0, min(room, share))
            budgets[k] += add
            given += add
        left -= given
        if given == 0:
            break
    return budgets
