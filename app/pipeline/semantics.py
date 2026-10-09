"""What each metric means, and the checks that keep a verified quote from being scored as the
wrong thing. A quote can be verbatim and still be the wrong metric (storage deployed read as
energy supplied; a bond offering read as capex), so after verification every figure passes
through ``check_figure``: it may be kept, reclassified to the metric the quote actually
describes (e.g. energy storage deployed — recorded, never scored), or rejected with a reason
that is stored on the evidence row and shown to admins."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .measures import parse_year

# Metric definitions: shown to the extraction model and on /methodology.
DEFINITIONS: dict[str, str] = {
    "energy_consumption": "Total energy the company itself CONSUMED in its own operations in a period "
                          "(electricity + fuels; MWh/GWh/TWh/GJ/TJ). Scored.",
    "electricity_consumption": "Electricity the company itself consumed/purchased for its own operations "
                               "(only when total energy is not given). Scored.",
    "energy_generated": "Energy the company GENERATED at plants it owns or operates (utilities, on-site "
                        "solar). Scored as throughput alongside consumption.",
    "energy_sold": "Energy sold or delivered to customers (retail/wholesale), including resold energy. "
                   "Recorded, not scored.",
    "energy_storage_deployed": "Battery/energy-storage capacity deployed, installed or shipped (e.g. "
                               "'deployed 46.7 GWh of energy storage'). NOT energy consumed or generated. "
                               "Recorded, not scored.",
    "datacenter_capacity_operating": "Data-center power capacity in operation today (MW/GW). Scored.",
    "datacenter_capacity_planned": "Announced, contracted or under-construction capacity (MW/GW). Recorded.",
    "capex": "Reported capital expenditure for a completed fiscal period: the cash-flow line 'purchases of "
             "property and equipment' or company-reported capex, from filings, IR or annual reports. Never "
             "bond/debt offerings, funding rounds, deal or order sizes, planned or forecast spend, or a "
             "number from a news headline.",
    "revenue": "Reported revenue for a completed fiscal period. Never forecasts, run-rates or valuations.",
}
SCORED_ENERGY = ("energy_consumption", "electricity_consumption", "energy_generated")
UNSCORED = ("energy_sold", "energy_storage_deployed", "datacenter_capacity_planned")
ENERGY_KEYS = ("energy_consumption", "electricity_consumption", "energy_generated", "energy_sold",
               "energy_storage_deployed", "energy_supplied")

_STORAGE = re.compile(
    r"(energy[- ]storage|storage (?:products?|systems?|deployments?|capacity|solutions?)|battery storage|"
    r"megapacks?|powerwalls?|stationary storage|batter(?:y|ies)\b[^.]{0,60}\bdeploy|"
    r"deploy\w*\b[^.]{0,60}\b(?:storage|batter(?:y|ies)))", re.IGNORECASE)
_SOLD = re.compile(r"\b(sold|sales? of (?:electricity|energy|power)|delivered to (?:customers|the grid)|"
                   r"retail (?:sales|deliveries)|deliveries to customers|customers? (?:consumed|used))\b",
                   re.IGNORECASE)
_CONSUME = re.compile(r"\b(consum\w*|usage|used|use of|purchased|electricity use|energy use)\b", re.IGNORECASE)
_GENERATE = re.compile(r"\b(generat\w*|produc\w*|output of our (?:plants|fleet))\b", re.IGNORECASE)

_CAPEX_POSITIVE = re.compile(
    r"(capital expenditures?|\bcapex\b|capital spending|capital investments?|purchases? of property|"
    r"property,? (?:plant,? )?and equipment|payments? (?:for|to acquire) (?:property|productive assets)|"
    r"additions to property|investments? in property)", re.IGNORECASE)
_FINANCING = re.compile(
    r"\b(bonds?|notes? (?:due|offering)|senior notes|debt|offering|loans?|credit (?:facility|line)|"
    r"financing|raised?|raising|ipo|valuation|funding round|series [a-h]\b|convertible|private placement)\b",
    re.IGNORECASE)
_DEAL = re.compile(
    r"\b(acqui(?:re|red|res|sition)|merger|deal|contract(?:s|ed)?|orders? (?:for|of|worth)|ordered|purchase agreement|"
    r"agreement to (?:buy|purchase)|turbines?|backlog|market (?:size|opportunity)|tam)\b", re.IGNORECASE)
_FORWARD = re.compile(
    r"\b(plans?|planned|planning|expects?|expected|anticipat\w+|intends?|will (?:spend|invest|be)|to spend|"
    r"guidance|outlook|forecasts?|forecasted|projected|projection|budget(?:ed)?|targets?|targeting|"
    r"commit(?:s|ted|ment)?|up to|over the next|proposed|pledged?)\b", re.IGNORECASE)
_RUNRATE = re.compile(r"\b(run[- ]rate|annuali[sz]ed|arr\b|valuation|valued at|bookings|backlog)\b", re.IGNORECASE)
_DC_FUTURE = re.compile(
    r"\b(planned|will|expected|under construction|upcoming|to be (?:built|completed|online)|proposed|"
    r"announced plans?|plans? to|planning to|intends? to|next year|pipeline|contracted|secured|"
    r"by (?:the end of )?20\d\d|coming online|under development|in development|being (?:developed|built)|"
    r"under way|underway|in progress|forthcoming|pre-construction|broke ground|breaking ground|"
    r"future capacity|development pipeline)\b", re.IGNORECASE)
_DC_OPERATING = re.compile(r"\b(operat\w*|online|on-line|in service|energi[sz]ed|running|live|installed|"
                           r"existing|currently|today|active)\b", re.IGNORECASE)
# Words that, right next to a capacity amount, mark it as future capacity (used by the summary guard).
_PLANNED_QUAL = re.compile(r"\b(planned|under (?:development|construction)|in development|pipeline|contracted|"
                           r"announced|future|proposed|by 20\d\d|upcoming|to come online)\b", re.IGNORECASE)

# A footnote marker *used* on a word or number ("capacity*", "3 GW†", "sqft¹") ...
_FOOTNOTE_USE = re.compile(r"(?<=[\w%)\]])(\*{1,3}|†|‡|§|[¹²³⁴⁵⁶⁷⁸⁹])")
_FOOTNOTE_CHARS = "*†‡§¹²³⁴⁵⁶⁷⁸⁹"
FOOTNOTE_WINDOW = 2500   # characters searched on each side of the quote (PDF text puts footnotes anywhere on the page)

_SCALE_CTX = re.compile(
    r"(?:\(|\b)(?:in|amounts in|dollars in|\$ in|us\$ in|usd in|expressed in)\s+(thousands|millions|billions)\b",
    re.IGNORECASE)
SCALE_UNITS = {"thousands": ("USD_thousands", 1e3), "millions": ("USD_millions", 1e6),
               "billions": ("USD_billions", 1e9)}
_INLINE_SCALE = re.compile(r"\b(thousand|million|billion|bn|mn)\b|\$\s?\d[\d,.]*\s?[mbk]\b", re.IGNORECASE)


@dataclass
class Verdict:
    ok: bool
    metric_key: str
    reason: str | None = None       # why rejected
    note: str | None = None         # why reclassified


def table_scale(text: str, pos: int, lookback: int = 8000) -> tuple[str, float] | None:
    """The '(in millions)' style scale declared for the table a figure sits in: the last such
    phrase in the ``lookback`` characters before ``pos``. Returns (currency unit, factor)."""
    window = text[max(0, pos - lookback): pos + 200]
    hits = list(_SCALE_CTX.finditer(window))
    if not hits:
        return None
    return SCALE_UNITS[hits[-1].group(1).lower()]


def _same_row(quote: str, row_context: str) -> str:
    """The part of ``row_context`` in the same sentence / table row as the quote: prose context is cut
    at sentence ends so a neighbouring sentence ("An additional 120 MW is planned.") does not leak in."""
    ctx = row_context or ""
    i = ctx.find(quote) if quote else -1
    if i < 0:
        return ctx
    left = max(ctx.rfind(". ", 0, i), ctx.rfind("? ", 0, i), ctx.rfind("! ", 0, i))
    right = [j for j in (ctx.find(". ", i + len(quote)), ctx.find("? ", i + len(quote))) if j >= 0]
    return ctx[left + 1 if left >= 0 else 0: min(right) + 1 if right else len(ctx)]


def footnote_markers(quote: str) -> set[str]:
    return set(_FOOTNOTE_USE.findall(quote or ""))


def footnote_texts(text: str, pos: int, quote: str, window: int = FOOTNOTE_WINDOW) -> list[str]:
    """Footnote definitions for the markers used in ``quote`` (found at ``pos`` in ``text``).

    A definition is the marker standing on its own (after whitespace or at the start) followed by text,
    e.g. "* Under development as of March 2026". Extracted PDF text puts footnotes before or after the
    figure, so ``window`` characters are searched on both sides. Several footnotes can share a marker
    (Crusoe uses "*" for two); all are returned and the caller treats any future-capacity wording in
    them conservatively."""
    out: list[str] = []
    for mk in footnote_markers(quote):
        lo = max(0, pos - window)
        hi = min(len(text), pos + len(quote) + window)
        stop = re.escape(_FOOTNOTE_CHARS)
        pat = re.compile(rf"(?:(?<=\s)|^){re.escape(mk)}(?![{stop}])\s*([A-Za-z(][^{stop}]{{3,160}})")
        for m in pat.finditer(text, lo, hi):
            if pos <= m.start() < pos + len(quote):
                continue
            out.append(m.group(1).strip())
    return out


def has_inline_scale(quote: str) -> bool:
    return bool(_INLINE_SCALE.search(quote or ""))


def check_figure(metric_key: str, *, quote: str, period: str | None, is_primary: bool,
                 current_year: int, row_context: str = "", footnotes: list[str] | tuple = ()) -> Verdict:
    """Semantic checks on one verified figure. ``row_context`` is the source text right around the
    quote (table row label, header) used only to confirm what the row is. ``footnotes`` are the
    definitions of footnote markers used in the quote (see ``footnote_texts``)."""
    q = quote or ""
    both = f"{q} {row_context}"
    year = parse_year(period)

    if metric_key in ENERGY_KEYS:
        if metric_key != "energy_storage_deployed" and _STORAGE.search(q):
            return Verdict(True, "energy_storage_deployed",
                           note="energy storage deployed is not energy consumed or generated (recorded, not scored)")
        if metric_key == "energy_supplied":   # legacy key: decide what the quote describes
            if _GENERATE.search(q) and not _SOLD.search(q):
                metric_key = "energy_generated"
            else:
                return Verdict(True, "energy_sold", note="energy supplied/delivered to customers is recorded, not scored")
        if metric_key in ("energy_consumption", "electricity_consumption"):
            if _SOLD.search(q) and not _CONSUME.search(q):
                return Verdict(True, "energy_sold", note="energy sold/delivered is not energy consumed")
            if _GENERATE.search(q) and not _CONSUME.search(q):
                return Verdict(True, "energy_generated", note="quote describes generation, not consumption")
        if metric_key == "energy_generated" and _SOLD.search(q) and not _GENERATE.search(q):
            return Verdict(True, "energy_sold", note="energy sold/delivered is not own generation")
        if metric_key in SCORED_ENERGY and year is not None and year > current_year:
            return Verdict(False, metric_key, "future period: a plan, not a reported figure")
        return Verdict(True, metric_key)

    if metric_key == "datacenter_capacity_operating":
        planned_note = "planned / under-construction capacity is recorded, not scored"
        if _DC_FUTURE.search(q) or (year is not None and year > current_year):
            return Verdict(True, "datacenter_capacity_planned", note=planned_note)
        if not _DC_OPERATING.search(q) and _DC_FUTURE.search(_same_row(q, row_context)):
            return Verdict(True, "datacenter_capacity_planned",
                           note=f"{planned_note} (the surrounding table row says so)")
        fn = next((t for t in footnotes if _DC_FUTURE.search(t)), None)
        if fn:
            return Verdict(True, "datacenter_capacity_planned",
                           note=f"{planned_note} (footnote: \"{fn[:80]}\")")
        if footnote_markers(q) and not footnotes and not _DC_OPERATING.search(q):
            return Verdict(True, "datacenter_capacity_planned",
                           note="footnoted capacity whose footnote could not be found is not treated as operating")
        return Verdict(True, metric_key)

    if metric_key == "capex":
        if _FINANCING.search(q):
            return Verdict(False, metric_key, "a financing amount (bond/debt/offering/funding) is not capex")
        if _DEAL.search(q) and not _CAPEX_POSITIVE.search(q):
            return Verdict(False, metric_key, "a deal, order or contract size is not reported capex")
        if not _CAPEX_POSITIVE.search(both):
            return Verdict(False, metric_key, "not a reported capital expenditure (no 'capital expenditures' / "
                                              "'purchases of property and equipment' in the quote)")
        if _FORWARD.search(q):
            return Verdict(False, metric_key, "planned or forecast spend is not reported capex")
        if not is_primary:
            return Verdict(False, metric_key, "capex is accepted only from company filings, IR or reports "
                                              "(primary sources), not news")
        if year is None:
            return Verdict(False, metric_key, "capex needs a fiscal period")
        if year > current_year:
            return Verdict(False, metric_key, "future period: planned spend, not reported capex")
        return Verdict(True, metric_key)

    if metric_key == "revenue":
        if _RUNRATE.search(q):
            return Verdict(False, metric_key, "run-rate, bookings or valuation is not reported revenue")
        if _FORWARD.search(q):
            return Verdict(False, metric_key, "forecast revenue is not reported revenue")
        if year is None:
            return Verdict(False, metric_key, "revenue needs a fiscal period")
        if year > current_year:
            return Verdict(False, metric_key, "future period: a forecast, not reported revenue")
        return Verdict(True, metric_key)

    return Verdict(True, metric_key)


# ---------------- capacity plausibility and summary guard ----------------

SECONDS_PER_YEAR = 3.15576e7
CAPACITY_MIN_UTILISATION = 0.2     # even a lightly used fleet draws >= 20% of its capacity on average
CAPACITY_ENERGY_TOLERANCE = 10.0   # and reported energy is not off by more than 10x


def capacity_inconsistent(capacity_w: float, capacity_year: int | None,
                          energy: list[tuple[float, int | None]]) -> str | None:
    """Why an *operating* capacity figure contradicts the company's reported annual energy (J/yr), or None.

    Operating capacity × hours in a year × 20% utilisation is a floor on the energy the fleet must use;
    if that floor is more than 10× the largest reported energy/electricity consumption for the same
    period (±1 year), the capacity is almost certainly planned or mis-scoped. Without a comparable
    energy figure nothing is said."""
    if not capacity_w or capacity_w <= 0:
        return None
    same = [j for j, y in energy if j and j > 0 and (capacity_year is None or y is None or abs(y - capacity_year) <= 1)]
    if not same:
        return None
    floor = capacity_w * SECONDS_PER_YEAR * CAPACITY_MIN_UTILISATION
    reported = max(same)
    if floor > CAPACITY_ENERGY_TOLERANCE * reported:
        avg_mw = reported / SECONDS_PER_YEAR / 1e6
        return (f"operating capacity {capacity_w / 1e6:,.0f} MW is inconsistent with reported energy use "
                f"(~{avg_mw:,.1f} MW average): even at {CAPACITY_MIN_UTILISATION:.0%} utilisation it would use "
                f"{floor / reported:,.0f}x the reported energy")
    return None


def _amount_patterns(mw: float) -> list[re.Pattern]:
    """Ways a capacity of ``mw`` megawatts is written in prose: "3 GW", "3GW", "3 gigawatts", "3,000 MW"."""
    pats = []
    def num(x: float) -> str:
        s = f"{x:,.2f}".rstrip("0").rstrip(".")
        return re.escape(s).replace(",", ",?")
    if mw >= 100:
        pats.append(rf"(?<![\d.]){num(mw / 1000)}\s*(?:GW|gigawatts?)\b")
    pats.append(rf"(?<![\d.]){num(mw)}\s*(?:MW|megawatts?)\b")
    return [re.compile(p, re.IGNORECASE) for p in pats]


def strip_future_as_current(text: str | None, future_mw: list[float]) -> tuple[str | None, int]:
    """Remove sentences that present future (planned / footnoted / inconsistent) capacity as operating.

    A sentence is removed when it mentions one of ``future_mw`` and operating/current wording, and the
    amount is not qualified as future right next to it ("3 GW planned", "planned 3 GW"). Returns the
    cleaned text (None if nothing is left) and the number of sentences removed."""
    if not text or not future_mw:
        return text, 0
    pats = [p for mw in future_mw if mw and mw > 0 for p in _amount_patterns(mw)]
    kept, removed = [], 0
    for sent in re.split(r"(?<=[.!?])\s+", text.strip()):
        bad = False
        for p in pats:
            for m in p.finditer(sent):
                near = sent[max(0, m.start() - 20): m.end() + 25]
                if _DC_OPERATING.search(sent) and not _PLANNED_QUAL.search(near):
                    bad = True
        if bad:
            removed += 1
        else:
            kept.append(sent)
    out = " ".join(kept).strip()
    return (out or None), removed
