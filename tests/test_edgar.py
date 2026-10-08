from app.pipeline.edgar import EdgarClient, annual_series, financials
from tests import fakes


def test_annual_series_full_years_latest_filing_wins():
    capex = annual_series(fakes.COMPANY_FACTS, "capex")
    assert [v.fiscal_year_end for v in capex] == ["2024-01-28", "2023-01-29", "2022-01-30", "2021-01-31"]
    assert capex[0].value == 1.331e9  # the 10-Q value is ignored
    rev = annual_series(fakes.COMPANY_FACTS, "revenue")
    assert rev[0].value == 27.0e9 and rev[0].concept == "Revenues"
    assert capex[0].filing_url("0001045810") == "https://www.sec.gov/Archives/edgar/data/1045810/000104581024000010/"


def test_missing_concepts_give_empty_series():
    assert financials({"facts": {}}) == {"capex": [], "revenue": [], "rnd": []}


def test_ticker_lookup_sends_contact_user_agent():
    seen = {}

    class F(fakes.FakeFetcher):
        def get(self, url, *, headers=None, max_bytes=None):
            seen["ua"] = (headers or {}).get("User-Agent")
            return super().get(url, headers=headers, max_bytes=max_bytes)
    client = EdgarClient(F(fakes.world_routes()), "Kardashev Index ops@example.com")
    assert client.lookup_ticker("nvda") == ("0001045810", "NVIDIA CORP")
    assert client.lookup_ticker("ZZZZ") is None
    assert seen["ua"] == "Kardashev Index ops@example.com"
