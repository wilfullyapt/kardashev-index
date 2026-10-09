"""Single source of truth for categories, weights, normalization anchors and rubrics.
The /methodology page renders straight from this module, so the published method can't drift
from the code that computes scores. Bump the version strings whenever anything here changes."""
from __future__ import annotations

import hashlib
import json

PIPELINE_VERSION = "pipeline-v2.5"
WEIGHTS_VERSION = "weights-v1"
RUBRIC_VERSION = "rubrics-v1"

MEASURED, JUDGED = "measured", "judged"

# Weights sum to 1.0: 65% measured (computed in code from reported figures), 35% judged.
CATEGORIES: list[dict] = [
    {
        "key": "energy_throughput", "family": MEASURED, "weight": 0.30,
        "label": "Energy throughput", "short": "Energy", "kicker": "Joules",
        "what": "Energy the company reports consuming in its own operations, or generating at plants it "
                "owns or operates, per year. The larger of the two is used. Energy storage deployed or "
                "shipped (e.g. battery GWh) and energy sold or resold to customers are recorded but never "
                "scored: they are not energy the company itself used or produced.",
        "inputs": "Reported annual energy consumption, electricity consumption or own generation "
                  "(MWh, GWh, TWh, GJ, TJ, PJ, MMBtu) with a verbatim quote from a fetched source.",
        "formula": "P = annual energy (J) / seconds per year → average watts. "
                   "Score = clamp(2.5 × (log₁₀P − 7), 0, 10). Kardashev-equivalent K = (log₁₀P − 6) / 10.",
        "anchors": [("10 MW avg (≈ 88 GWh/yr)", 0), ("100 MW (≈ 0.88 TWh/yr)", 2.5), ("1 GW (≈ 8.8 TWh/yr)", 5),
                    ("10 GW (≈ 88 TWh/yr)", 7.5), ("100 GW (≈ 876 TWh/yr)", 10)],
    },
    {
        "key": "compute_capacity", "family": MEASURED, "weight": 0.20,
        "label": "Compute capacity", "short": "Compute", "kicker": "Watts of thought",
        "what": "Data-center capacity the company reports operating (IT or facility power, MW). "
                "Announced or under-construction capacity is shown but not scored.",
        "inputs": "Reported operating data-center capacity in MW or GW with a verbatim quote.",
        "formula": "Score = clamp(2.5 × log₁₀(MW), 0, 10).",
        "anchors": [("1 MW", 0), ("10 MW", 2.5), ("100 MW", 5), ("1 GW", 7.5), ("10 GW", 10)],
    },
    {
        "key": "growth", "family": MEASURED, "weight": 0.15,
        "label": "Growth gradient", "short": "Growth", "kicker": "Slope",
        "what": "How fast the company is building: capital-expenditure trend (from SEC filings when "
                "available), revenue trend, and reported energy-use growth.",
        "inputs": "Annual capex and revenue: SEC XBRL company facts (API URL + accession number) first, else "
                  "the cash-flow line 'purchases of property and equipment' or company-reported capex quoted "
                  "from a filing, IR page or annual report. Bonds, funding rounds, deal sizes, planned spend "
                  "and headlines are rejected. Energy series as above.",
        "formula": "Each available sub-metric's compound annual growth rate (up to 3 years) is mapped "
                   "piecewise-linearly through the anchors, then combined with weights capex 50%, "
                   "revenue 25%, energy 25% (renormalized over what is available).",
        "anchors": [("−30%/yr or worse", 0), ("0%/yr", 3), ("+10%/yr", 5), ("+25%/yr", 7), ("+50%/yr", 9),
                    ("+100%/yr or more", 10)],
    },
    {
        "key": "frontier_acceleration", "family": JUDGED, "weight": 0.15,
        "label": "Frontier acceleration", "short": "Frontier", "kicker": "Opinion · Capability",
        "what": "Contribution to pushing the technological frontier: new capabilities shipped, "
                "cost-per-capability reductions, open releases others build on.",
    },
    {
        "key": "builder_velocity", "family": JUDGED, "weight": 0.12,
        "label": "Builder velocity", "short": "Build", "kicker": "Opinion · Shipping",
        "what": "How quickly the company turns plans into physical or shipped reality: cadence, "
                "first-of-a-kind deployments, time from announcement to operation.",
    },
    {
        "key": "policy_stance", "family": JUDGED, "weight": 0.08,
        "label": "Permission to build", "short": "Policy", "kicker": "Opinion · Policy",
        "what": "Public positions and actions on permitting, energy build-out, open source and "
                "regulation. This is explicitly an editorial e/acc lens and carries the lowest weight.",
    },
]

RUBRICS: dict[str, dict[int, str]] = {
    "frontier_acceleration": {
        0: "No evidence of contributing new capability; follows others or is outside technology entirely.",
        2: "Incremental improvements to existing products; little that others can build on.",
        4: "Competitive, current products in a frontier field, but rarely first; modest cost or capability gains.",
        6: "Regularly ships capabilities that are near state of the art, or materially lowers cost per capability.",
        8: "Repeatedly first or best on important capability measures, or releases that a wide ecosystem builds on.",
        10: "Defines the frontier of its field: step-change capabilities or cost collapses that reset the industry.",
    },
    "builder_velocity": {
        0: "No evidence of shipping or building anything new in the period.",
        2: "Slow cadence; announcements routinely slip by years or are abandoned.",
        4: "Steady, ordinary cadence for its industry.",
        6: "Faster than industry norms; several meaningful launches or facilities brought online on schedule.",
        8: "Exceptional speed: first-of-a-kind deployments or record build times, repeatedly.",
        10: "Sets the global benchmark for speed at scale; compresses multi-year industry timelines into months.",
    },
    "policy_stance": {
        0: "Actively lobbies to restrict building, energy supply or open technology for others.",
        2: "Mostly supports restrictive positions; occasional pro-build statements.",
        4: "Neutral or mixed public record; little substantive advocacy either way.",
        6: "Generally supports permitting reform, energy build-out or open technology in public positions.",
        8: "Consistent, substantive advocacy and action for building, abundant energy and open technology.",
        10: "Leading, sustained advocate whose actions measurably expanded others' permission to build.",
    },
}

CATEGORY_KEYS = [c["key"] for c in CATEGORIES]
MEASURED_KEYS = [c["key"] for c in CATEGORIES if c["family"] == MEASURED]
JUDGED_KEYS = [c["key"] for c in CATEGORIES if c["family"] == JUDGED]
WEIGHTS = {c["key"]: c["weight"] for c in CATEGORIES}
BY_KEY = {c["key"]: c for c in CATEGORIES}

assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9
assert abs(sum(WEIGHTS[k] for k in MEASURED_KEYS) - 0.65) < 1e-9


def bundle_hash(*texts: str) -> str:
    """Hash of prompts + rubrics + weights, stored on every run."""
    payload = json.dumps({"weights": WEIGHTS, "rubrics": RUBRICS, "texts": texts,
                          "versions": [PIPELINE_VERSION, WEIGHTS_VERSION, RUBRIC_VERSION]}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()
