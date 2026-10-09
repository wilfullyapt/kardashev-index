"""Measured categories, computed in code from verified reported figures.
No LLM arithmetic: the model only copies numbers + units + a verbatim quote; this module
converts units, derives average power / Kardashev-equivalent, growth rates and 0-10 scores."""
from __future__ import annotations

import itertools
import math
import re
import statistics
from dataclasses import dataclass, field

SECONDS_PER_YEAR = 365.25 * 24 * 3600

ENERGY_TO_J = {
    "Wh": 3.6e3, "kWh": 3.6e6, "MWh": 3.6e9, "GWh": 3.6e12, "TWh": 3.6e15, "PWh": 3.6e18,
    "J": 1.0, "kJ": 1e3, "MJ": 1e6, "GJ": 1e9, "TJ": 1e12, "PJ": 1e15, "EJ": 1e18,
    "MMBtu": 1.055056e9,
}
POWER_TO_W = {"W": 1.0, "kW": 1e3, "MW": 1e6, "GW": 1e9, "TW": 1e12}
CURRENCY_TO_USD = {"USD": 1.0, "USD_thousands": 1e3, "USD_millions": 1e6, "USD_billions": 1e9}

METRIC_UNITS = {
    "energy_consumption": ENERGY_TO_J,
    "electricity_consumption": ENERGY_TO_J,
    "energy_generated": ENERGY_TO_J,
    "energy_sold": ENERGY_TO_J,
    "energy_storage_deployed": ENERGY_TO_J,
    "energy_supplied": ENERGY_TO_J,       # legacy (v2.0/v2.1 runs); no longer requested
    "datacenter_capacity_operating": POWER_TO_W,
    "datacenter_capacity_planned": POWER_TO_W,
    "capex": CURRENCY_TO_USD,
    "revenue": CURRENCY_TO_USD,
}
METRIC_LABELS = {
    "energy_consumption": "Energy consumed",
    "electricity_consumption": "Electricity consumed",
    "energy_generated": "Energy generated (own plants)",
    "energy_sold": "Energy sold / delivered (not scored)",
    "energy_storage_deployed": "Energy storage deployed (not scored)",
    "energy_supplied": "Energy supplied (legacy)",
    "datacenter_capacity_operating": "Data-center capacity (operating)",
    "datacenter_capacity_planned": "Data-center capacity (planned)",
    "capex": "Capital expenditure",
    "revenue": "Revenue",
}
GROWTH_ANCHORS = [(-0.30, 0.0), (0.0, 3.0), (0.10, 5.0), (0.25, 7.0), (0.50, 9.0), (1.00, 10.0)]
GROWTH_SUB_WEIGHTS = {"capex": 0.50, "revenue": 0.25, "energy": 0.25}


_SCALE_WORDS = {"thousand": 1e3, "thousands": 1e3, "k": 1e3, "'000": 1e3, "000": 1e3, "000s": 1e3,
                "million": 1e6, "millions": 1e6, "mn": 1e6, "mm": 1e6, "m": 1e6,
                "billion": 1e9, "billions": 1e9, "bn": 1e9, "b": 1e9}
_UNIT_WORDS = {
    "megawatthour": "MWh", "gigawatthour": "GWh", "terawatthour": "TWh", "kilowatthour": "kWh",
    "petawatthour": "PWh", "watthour": "Wh", "gigajoule": "GJ", "terajoule": "TJ", "petajoule": "PJ",
    "megajoule": "MJ", "exajoule": "EJ", "kilojoule": "kJ", "joule": "J", "megawatt": "MW",
    "gigawatt": "GW", "kilowatt": "kW", "terawatt": "TW", "watt": "W", "millionbtu": "MMBtu",
    "usd": "USD", "$": "USD", "us$": "USD", "dollar": "USD", "usdollar": "USD",
}
_PER_YEAR = re.compile(r"\s*(?:/|per)\s*(?:yr|year|annum|a)\b\.?|\s*(?:annually|a year|each year|p\.a\.)$")


def canonical_unit(metric_key: str, unit: str) -> str | None:
    """Map the unit as written ('MWh', 'megawatt hours', 'MWh/yr', 'thousand MWh', '$ millions')
    to a unit of this metric's table. A scale word is folded into the unit (thousand MWh = GWh)."""
    table = METRIC_UNITS.get(metric_key) or {}
    u = (unit or "").strip()
    if u in table:
        return u
    low = {k.lower(): k for k in table}
    if u.lower().replace(" ", "") in low:
        return low[u.lower().replace(" ", "")]
    t = _PER_YEAR.sub("", u.lower().replace("\u00a0", " ")).strip()
    t = re.sub(r"[()\[\]]", " ", t)
    t = re.sub(r"\bin\b|\bof\b", " ", t)
    words = [w for w in re.split(r"[\s,]+", t) if w]
    scale = 1.0
    rest = []
    for w in words:
        if w in _SCALE_WORDS and (rest or len(words) > 1):
            scale *= _SCALE_WORDS[w]
        else:
            rest.append(w)
    base_txt = "".join(rest).replace("-", "").replace(".", "")
    if base_txt.startswith(("us$", "$")) and len(base_txt) > len("$") and base_txt.lstrip("us$") in ("m", "mn", "bn", "b", "k"):
        scale *= _SCALE_WORDS[base_txt.lstrip("us$")]
        base_txt = "$"
    base_txt = base_txt[:-1] if base_txt.endswith("s") and base_txt[:-1] in _UNIT_WORDS else base_txt
    base = _UNIT_WORDS.get(base_txt) or low.get(base_txt)
    if base is None:
        return None
    if base == "USD" and table is CURRENCY_TO_USD:
        factor = scale
    elif base in table:
        factor = table[base] * scale
    else:
        return None
    for k, v in table.items():
        if abs(v - factor) <= 1e-9 * factor:
            return k
    return None


def to_si(metric_key: str, value: float, unit: str) -> float | None:
    u = canonical_unit(metric_key, unit)
    if u is None:
        return None
    return float(value) * METRIC_UNITS[metric_key][u]


def fmt_num(v: float | None, digits: int = 2) -> str:
    """Human number: thousands separators, never scientific notation (1053479 -> '1,053,479')."""
    if v is None:
        return "—"
    v = float(v)
    if v == int(v) and abs(v) < 1e15:
        return f"{int(v):,}"
    if abs(v) >= 100:
        return f"{v:,.0f}" if abs(v) >= 1e4 else f"{v:,.1f}"
    return f"{v:,.{digits}f}".rstrip("0").rstrip(".")


def clamp(x: float, lo: float = 0.0, hi: float = 10.0) -> float:
    return max(lo, min(hi, x))


def avg_power_w(joules_per_year: float) -> float:
    return joules_per_year / SECONDS_PER_YEAR


def kardashev(power_w: float) -> float:
    """Sagan's interpolation K = (log10 P - 6) / 10, P in watts."""
    return (math.log10(power_w) - 6) / 10


def energy_score(power_w: float) -> float:
    return clamp(2.5 * (math.log10(power_w) - 7)) if power_w > 0 else 0.0


def compute_score(megawatts: float) -> float:
    return clamp(2.5 * math.log10(megawatts)) if megawatts > 0 else 0.0


def growth_score(rate: float) -> float:
    pts = GROWTH_ANCHORS
    if rate <= pts[0][0]:
        return pts[0][1]
    if rate >= pts[-1][0]:
        return pts[-1][1]
    for (x0, y0), (x1, y1) in itertools.pairwise(pts):
        if x0 <= rate <= x1:
            return y0 + (y1 - y0) * (rate - x0) / (x1 - x0)
    return pts[-1][1]


def cagr(series: list[tuple[int, float]]) -> tuple[float, int] | None:
    """series: (year, value). Uses the latest year and the earliest within 3 years of it."""
    pts = sorted({y: v for y, v in series if v is not None and v > 0}.items())
    if len(pts) < 2:
        return None
    latest_y, latest_v = pts[-1]
    base = [(y, v) for y, v in pts if latest_y - 3 <= y < latest_y]
    if not base:
        return None
    y0, v0 = base[0]
    years = latest_y - y0
    return (latest_v / v0) ** (1 / years) - 1, years


def parse_year(period: str | None) -> int | None:
    m = re.search(r"(19|20)\d{2}", period or "")
    if m:
        return int(m.group(0))
    m = re.search(r"\bFY\s?'?(\d{2})\b", period or "", re.IGNORECASE)
    return 2000 + int(m.group(1)) if m else None


@dataclass
class Figure:
    evidence_id: int | None
    source_id: int | None
    metric_key: str
    value: float
    unit: str
    period: str | None
    is_primary: bool
    source_url: str | None = None
    quote: str | None = None
    origin: str = "quote"   # quote | edgar

    @property
    def year(self) -> int | None:
        return parse_year(self.period)

    @property
    def si(self) -> float | None:
        return to_si(self.metric_key, self.value, self.unit)


@dataclass
class MeasuredResult:
    key: str
    score: float | None
    confidence: float
    rationale: str
    inputs: dict = field(default_factory=dict)
    evidence_ids: list[int] = field(default_factory=list)


def _pick(figs: list[Figure]) -> tuple[Figure | None, bool]:
    """Latest year wins; primary sources preferred; median within the group. Returns (figure, conflict)."""
    usable = [f for f in figs if f.si is not None and f.si > 0]
    if not usable:
        return None, False
    latest = max((f.year or 0) for f in usable)
    group = [f for f in usable if (f.year or 0) == latest]
    prim = [f for f in group if f.is_primary] or group
    prim.sort(key=lambda f: f.si)
    chosen = prim[len(prim) // 2]
    vals = [f.si for f in group]
    conflict = len(vals) > 1 and (max(vals) / min(vals) > 1.25)
    return chosen, conflict


def _figure_conf(f: Figure, current_year: int, conflict: bool) -> float:
    c = 0.95 if f.origin == "edgar" else (0.9 if f.is_primary else 0.65)
    if f.year is None:
        c *= 0.7
    elif f.year < current_year - 3:
        c *= 0.75
    if conflict:
        c *= 0.8
    return c


def _fmt_energy(j: float) -> str:
    twh = j / 3.6e15
    return f"{twh:,.2f} TWh/yr" if twh >= 1 else f"{twh * 1000:,.0f} GWh/yr"


def fmt_power(w: float) -> str:
    for unit, scale in (("TW", 1e12), ("GW", 1e9), ("MW", 1e6), ("kW", 1e3)):
        if w >= scale:
            return f"{w / scale:,.1f} {unit}" if w / scale < 100 else f"{w / scale:,.0f} {unit}"
    return f"{w:,.0f} W"


def measure_energy(figs: list[Figure], current_year: int,
                   count_generation: bool = True) -> tuple[MeasuredResult, dict | None]:
    """Energy throughput from energy the company itself consumes (or, optionally, generates at its
    own plants). Energy sold to customers and storage deployed are recorded but never scored."""
    by_key = {k: [f for f in figs if f.metric_key == k] for k in
              ("energy_consumption", "electricity_consumption", "energy_generated")}
    consumed, c_conflict = _pick(by_key["energy_consumption"])
    electricity_only = False
    if consumed is None:
        consumed, c_conflict = _pick(by_key["electricity_consumption"])
        electricity_only = consumed is not None
    generated, g_conflict = _pick(by_key["energy_generated"]) if count_generation else (None, False)
    options = [(f, conflict, kind) for f, conflict, kind in
               ((consumed, c_conflict, "consumed"), (generated, g_conflict, "generated")) if f is not None]
    if not options:
        msg = "No verified annual energy-consumption figure was found, so this category is unscored."
        side = [f for f in figs if f.metric_key in ("energy_storage_deployed", "energy_sold", "energy_supplied")]
        if side:
            msg += (" Reported " + ", ".join(sorted({METRIC_LABELS[f.metric_key].split(" (")[0].lower()
                                                     for f in side}))
                    + " figures are recorded but are not energy the company consumes.")
        return MeasuredResult("energy_throughput", None, 0.0, msg), None
    f, conflict, kind = max(options, key=lambda o: o[0].si)
    p = avg_power_w(f.si)
    conf = _figure_conf(f, current_year, conflict) * (0.9 if electricity_only and kind == "consumed" else 1.0)
    score = round(energy_score(p), 2)
    k = kardashev(p)
    what = "electricity consumed" if electricity_only and kind == "consumed" else f"energy {kind}"
    rationale = (f"Reported {what}: {fmt_num(f.value)} {f.unit} ({f.period or 'period not stated'}) = "
                 f"{_fmt_energy(f.si)} → {fmt_power(p)} average → score {score:.1f}, K = {k:.3f}.")
    if electricity_only:
        rationale += " Electricity only (fuels not included), so this likely understates total energy."
    if conflict:
        rationale += " Sources disagreed by more than 25% for this period; confidence reduced."
    headline = {"avg_power_w": p, "k_equivalent": k, "joules_per_year": f.si, "kind": what,
                "period": f.period, "year": f.year, "source_url": f.source_url, "evidence_id": f.evidence_id,
                "value": f.value, "unit": f.unit, "display_energy": _fmt_energy(f.si), "display_power": fmt_power(p)}
    inputs = {"metric_key": f.metric_key, "value": f.value, "unit": f.unit, "period": f.period,
              "joules_per_year": f.si, "avg_power_w": p, "k_equivalent": k, "source_url": f.source_url,
              "electricity_only": electricity_only, "conflict": conflict, "basis": kind}
    ev = [f.evidence_id] if f.evidence_id else []
    return MeasuredResult("energy_throughput", score, round(conf, 3), rationale, inputs, ev), headline


def measure_compute(figs: list[Figure], current_year: int) -> MeasuredResult:
    f, conflict = _pick([x for x in figs if x.metric_key == "datacenter_capacity_operating"])
    planned, _ = _pick([x for x in figs if x.metric_key == "datacenter_capacity_planned"])
    extra = {}
    if planned is not None:
        extra = {"planned_mw": planned.si / 1e6, "planned_period": planned.period,
                 "planned_source_url": planned.source_url}
    if f is None:
        msg = "No verified operating data-center capacity was found, so this category is unscored."
        if planned is not None:
            msg += f" Planned capacity of {fmt_power(planned.si)} is reported but not scored."
        return MeasuredResult("compute_capacity", None, 0.0, msg, extra)
    mw = f.si / 1e6
    score = round(compute_score(mw), 2)
    conf = _figure_conf(f, current_year, conflict)
    rationale = (f"Reported operating data-center capacity: {fmt_num(f.value)} {f.unit} ({f.period or 'period not stated'})"
                 f" = {mw:,.0f} MW → score {score:.1f}.")
    inputs = {"value": f.value, "unit": f.unit, "period": f.period, "megawatts": mw, "source_url": f.source_url,
              "conflict": conflict, **extra}
    ev = [f.evidence_id] if f.evidence_id else []
    return MeasuredResult("compute_capacity", score, round(conf, 3), rationale, inputs, ev)


def _series(figs: list[Figure]) -> tuple[list[tuple[int, float]], list[Figure]]:
    by_year: dict[int, Figure] = {}
    for f in figs:
        if f.year is None or f.si is None or f.si <= 0:
            continue
        cur = by_year.get(f.year)
        rank = (f.origin == "edgar", f.is_primary)
        if cur is None or rank > (cur.origin == "edgar", cur.is_primary):
            by_year[f.year] = f
    pts = sorted(by_year.items())
    return [(y, f.si) for y, f in pts], [f for _, f in pts]


def measure_growth(figs: list[Figure]) -> MeasuredResult:
    subs: dict[str, dict] = {}
    groups = {
        "capex": [f for f in figs if f.metric_key == "capex"],
        "revenue": [f for f in figs if f.metric_key == "revenue"],
    }
    energy = [f for f in figs if f.metric_key == "energy_consumption"] or \
        [f for f in figs if f.metric_key == "electricity_consumption"] or \
        [f for f in figs if f.metric_key == "energy_generated"]
    groups["energy"] = energy
    ev: list[int] = []
    for name, group in groups.items():
        series, used = _series(group)
        g = cagr(series)
        if g is None:
            continue
        rate, years = g
        base = statistics.mean(0.95 if f.origin == "edgar" else (0.85 if f.is_primary else 0.65) for f in used)
        subs[name] = {"cagr": rate, "years": years, "score": growth_score(rate), "base_conf": base,
                      "series": [{"year": f.year, "value_usd" if name != "energy" else "joules": f.si,
                                  "origin": f.origin, "source_url": f.source_url} for f in used]}
        ev += [f.evidence_id for f in used if f.evidence_id]
    if not subs:
        return MeasuredResult("growth", None, 0.0,
                              "Fewer than two comparable annual figures were found, so growth is unscored.")
    wsum = sum(GROWTH_SUB_WEIGHTS[k] for k in subs)
    score = round(sum(GROWTH_SUB_WEIGHTS[k] * v["score"] for k, v in subs.items()) / wsum, 2)
    conf = sum(GROWTH_SUB_WEIGHTS[k] * v["base_conf"] for k, v in subs.items())
    parts = [f"{k} {v['cagr'] * 100:+.0f}%/yr over {v['years']}y" for k, v in subs.items()]
    rationale = "Compound annual growth: " + ", ".join(parts) + f" → score {score:.1f}."
    missing = [k for k in GROWTH_SUB_WEIGHTS if k not in subs]
    if missing:
        rationale += f" Not available: {', '.join(missing)} (confidence reduced)."
    return MeasuredResult("growth", score, round(conf, 3), rationale, {"subs": subs}, ev)


def measure_all(figs: list[Figure], current_year: int,
                count_generation: bool = True) -> tuple[list[MeasuredResult], dict | None]:
    energy, headline = measure_energy(figs, current_year, count_generation)
    return [energy, measure_compute(figs, current_year), measure_growth(figs)], headline
