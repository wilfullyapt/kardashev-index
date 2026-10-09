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
    r"by (?:the end of )?20\d\d|coming online)\b", re.IGNORECASE)

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


def has_inline_scale(quote: str) -> bool:
    return bool(_INLINE_SCALE.search(quote or ""))


def check_figure(metric_key: str, *, quote: str, period: str | None, is_primary: bool,
                 current_year: int, row_context: str = "") -> Verdict:
    """Semantic checks on one verified figure. ``row_context`` is the source text right around the
    quote (table row label, header) used only to confirm what the row is."""
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
        if _DC_FUTURE.search(q) or (year is not None and year > current_year):
            return Verdict(True, "datacenter_capacity_planned",
                           note="planned / under-construction capacity is recorded, not scored")
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
