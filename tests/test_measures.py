"""Unit conversion, Kardashev math, normalization anchors and measured-category selection."""
import math

import pytest

from app.pipeline import measures as M
from app.pipeline.measures import Figure


def fig(key, value, unit, period="2024", primary=True, origin="quote", eid=1):
    return Figure(eid, 1, key, value, unit, period, primary, "https://x.example/r", "q", origin)


@pytest.mark.parametrize("value,unit,joules", [
    (1, "TWh", 3.6e15), (1000, "GWh", 3.6e15), (1e6, "MWh", 3.6e15), (1e9, "kWh", 3.6e15),
    (3.6, "PJ", 3.6e15), (3.6e6, "GJ", 3.6e15), (1, "MMBtu", 1.055056e9), (2, "mwh", 7.2e9),
])
def test_energy_unit_conversion(value, unit, joules):
    assert M.to_si("energy_consumption", value, unit) == pytest.approx(joules)


def test_unknown_or_cross_family_units_are_refused():
    assert M.to_si("energy_consumption", 5, "MW") is None
    assert M.to_si("datacenter_capacity_operating", 5, "MWh") is None
    assert M.to_si("capex", 5, "EUR") is None
    assert M.to_si("capex", 2.5, "USD_billions") == 2.5e9


def test_average_power_and_kardashev():
    one_gw_year = 1e9 * M.SECONDS_PER_YEAR
    assert M.avg_power_w(one_gw_year) == pytest.approx(1e9)
    assert M.kardashev(1e9) == pytest.approx(0.3)
    assert M.kardashev(1e16) == pytest.approx(1.0)       # Type I
    assert M.kardashev(2e13) == pytest.approx(0.73, abs=0.01)  # humanity


@pytest.mark.parametrize("watts,score", [(1e7, 0), (1e8, 2.5), (1e9, 5), (1e10, 7.5), (1e11, 10), (1e6, 0), (1e13, 10)])
def test_energy_score_anchors(watts, score):
    assert M.energy_score(watts) == pytest.approx(score)


@pytest.mark.parametrize("mw,score", [(1, 0), (10, 2.5), (100, 5), (1000, 7.5), (10000, 10), (0.5, 0), (1e6, 10)])
def test_compute_score_anchors(mw, score):
    assert M.compute_score(mw) == pytest.approx(score)


@pytest.mark.parametrize("rate,score", [(-0.5, 0), (-0.30, 0), (0, 3), (0.05, 4), (0.10, 5), (0.25, 7),
                                        (0.375, 8), (0.5, 9), (1.0, 10), (3.0, 10)])
def test_growth_anchors_piecewise_linear(rate, score):
    assert M.growth_score(rate) == pytest.approx(score)


def test_cagr_uses_latest_and_up_to_three_years_back():
    rate, years = M.cagr([(2018, 1), (2021, 1.0), (2024, 1.331)])
    assert years == 3 and rate == pytest.approx(0.10)
    assert M.cagr([(2024, 5)]) is None
    assert M.cagr([(2023, 0), (2024, 5)]) is None


@pytest.mark.parametrize("period,year", [("FY2024", 2024), ("2023", 2023), ("fiscal 2022", 2022), ("FY24", 2024),
                                         (None, None), ("recent", None)])
def test_parse_year(period, year):
    assert M.parse_year(period) == year


def test_energy_uses_latest_year_prefers_primary_and_max_of_consumed_supplied():
    figs = [fig("energy_consumption", 400, "GWh", "2022"), fig("energy_consumption", 500, "GWh", "2024", primary=False),
            fig("energy_consumption", 480, "GWh", "2024", primary=True, eid=7)]
    res, _ = M.measure_energy(figs, 2026)
    assert res.inputs["value"] == 480 and res.evidence_ids == [7]
    assert res.confidence == pytest.approx(0.9)  # primary, recent, 500 vs 480 is not a conflict
    supplied = figs + [fig("energy_supplied", 50, "TWh", "2024", eid=9)]
    res2, head2 = M.measure_energy(supplied, 2026)
    assert res2.inputs["basis"] == "supplied" and head2["avg_power_w"] == pytest.approx(50 * 3.6e15 / M.SECONDS_PER_YEAR)


def test_energy_confidence_penalties():
    res, _ = M.measure_energy([fig("electricity_consumption", 1, "TWh", "2019", primary=False)], 2026)
    assert res.confidence == pytest.approx(0.65 * 0.75 * 0.9, abs=1e-3)
    assert "Electricity only" in res.rationale
    conflict, _ = M.measure_energy([fig("energy_consumption", 1, "TWh"), fig("energy_consumption", 2, "TWh")], 2026)
    assert conflict.inputs["conflict"] is True and "disagreed" in conflict.rationale


def test_missing_data_is_none_not_five():
    for res in M.measure_all([], 2026)[0]:
        assert res.score is None and res.confidence == 0


def test_compute_ignores_planned_capacity_for_score():
    res = M.measure_compute([fig("datacenter_capacity_planned", 1, "GW")], 2026)
    assert res.score is None and "not scored" in res.rationale and res.inputs["planned_mw"] == 1000
    res = M.measure_compute([fig("datacenter_capacity_operating", 2, "GW")], 2026)
    assert res.score == pytest.approx(2.5 * math.log10(2000), abs=0.01)


def test_growth_combines_subs_and_prefers_edgar():
    figs = [fig("capex", 1.0, "USD_billions", "FY2021", origin="edgar"), fig("capex", 1.331, "USD_billions", "FY2024", origin="edgar"),
            fig("capex", 9.0, "USD_billions", "FY2024", origin="quote"),  # loses to EDGAR for the same year
            fig("revenue", 10, "USD_billions", "FY2021", origin="edgar"), fig("revenue", 27, "USD_billions", "FY2024", origin="edgar")]
    res = M.measure_growth(figs)
    assert res.inputs["subs"]["capex"]["cagr"] == pytest.approx(0.10)
    rev = M.growth_score(2.7 ** (1 / 3) - 1)
    assert res.score == pytest.approx((0.5 * 5 + 0.25 * rev) / 0.75, abs=0.01)
    assert res.confidence == pytest.approx(0.75 * 0.95, abs=1e-3) and "energy" in res.rationale
