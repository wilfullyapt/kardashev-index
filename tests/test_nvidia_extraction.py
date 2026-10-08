"""Regression tests for the 2026-10-08 NVIDIA production run that published "0% of weight scored"
although NVIDIA's FY26 sustainability report (with its energy table) had been fetched.

Root causes reproduced here with a real pypdf text excerpt of that report (tests/fixtures):
  * table rows such as "Energy consumption 1,053,479 815,864 593,953" were parsed as ONE number
    (space treated as a thousands separator), so every table figure failed "value in quote";
  * verbatim matching broke on PDF artifacts (hyphenation splits, footnote markers) and units
    spelled out ("megawatt hours") were unsupported;
  * extraction windows sent ~5% of a long report, ranked by generic words, cut mid-sentence and
    favoured boilerplate (forward-looking statements, navigation).
"""
from pathlib import Path

import pytest

from app.pipeline import evidence as ev
from app.pipeline.fetch import _norm_ws, extract_text
from app.pipeline.measures import canonical_unit

FIXTURE = Path(__file__).parent / "fixtures" / "nvidia_fy26_excerpt.txt"
ROW = "Energy (MWh)* Energy consumption 1,053,479 815,864 593,953"


def pages() -> list[str]:
    raw = FIXTURE.read_text(encoding="utf-8")
    body = "\n".join(line for line in raw.split("\n") if not line.startswith("# "))
    return body.split("\f")


@pytest.fixture(scope="module")
def src() -> ev.SourceText:
    # exactly how fetch.extract_text turns PDF pages into text
    return ev.SourceText(_norm_ws(" ".join(pages())))


def test_fixture_is_real_pypdf_text_with_its_artifacts():
    raw = "\f".join(pages())
    assert "trillion-\nparameter" in raw            # hyphenation split across lines
    assert "emissions.1 NVIDIA" in raw.replace("\n", " ")   # footnote marker glued to a word
    assert ROW.split("* ")[1] in raw and "Metric FY26 FY25 FY24" in raw
    assert len(raw) < 8000                            # an excerpt, not the PDF


def test_table_row_yields_each_cell_not_one_giant_number():
    assert ev.numbers_in(ROW)[:3] == [1053479.0, 815864.0, 593953.0]
    for v in (1053479, 815864, 593953):
        assert ev.number_matches(v, ROW)
    assert ev.number_matches(622818, "622 818")       # genuine space-grouped thousands still read
    assert ev.number_matches(1234567.5, "1 234 567.5 GJ")
    assert ev.number_matches(1053479, "1.053.479 MWh")  # dotted thousands
    assert not ev.number_matches(1100000, ROW)


@pytest.mark.parametrize("quote,method", [
    (ROW, "exact"),
    ("Energy consumption 1,053,479 815,864 593,953", "exact"),
    ("Energy consumption 1\u00a0053\u2009479", "loose"),              # NBSP / thin-space grouping
    ("for real-time trillion-parameter inference and training", "loose"),  # PDF split "trillion- parameter"
    ("NVIDIA's sustainability reporting follows our fiscal calendar", "exact"),  # straight vs curly apostrophe
    ("has demonstrated up to 10x energy efficiency over the previous architecture, software updates continue",
     "footnote"),                                                       # source: "architecture,3 software"
    ("reducing Scope 2 market-based emissions. NVIDIA defines clean electricity", "footnote"),
    (("Last year, we met our goal to purchase or generate enough clean electricity ... for sites under our "
      "operational control"), "ellipsis"),
    ("In FY26, we continued to match 100% of our\nglobal electricity usage with clean electricity", "exact"),
])
def test_tolerant_matching_of_real_pdf_text(src, quote, method):
    m = ev.locate(quote, src)
    assert m is not None, quote
    assert m.method == method
    assert m.text in _norm_ws(src.text)               # what we store is the source's own text


def test_stored_quote_is_the_source_span(src):
    m = ev.locate("for real-time trillion-parameter inference and training.", src)
    assert m.text == "for real-time trillion- parameter inference and training."
    m = ev.locate("Last year, we met our goal … operational control.", src)
    assert m.text.startswith("Last year, we met our goal to purchase") and m.text.endswith("operational control.")


@pytest.mark.parametrize("quote", [
    "Energy consumption 1,053,497 815,864 593,953",      # one digit changed
    "Energy consumption 1,053,479 815,864 593,935",
    "NVIDIA matched all of its electricity with clean power in FY26",   # paraphrase
    "NVIDIA has lobbied for faster permitting of new power plants.",    # invented
    "Energy consumption",                                # too short to mean anything
    "Last year, we met our goal … NVIDIA lobbies for permitting reform",  # second piece invented
])
def test_changed_digits_paraphrases_and_inventions_still_rejected(src, quote):
    assert ev.locate(quote, src) is None


def test_loose_match_never_changes_the_value(src):
    # punctuation-free matching would equate "4.9 6.3 9.7" with "49 63 97"; the value is checked
    # against the source span, so the decimal point still matters
    m = ev.locate("Energy intensity (Energy consumption MWh/$M revenue) 49 63 97", src)
    assert m is not None and "4.9 6.3 9.7" in m.text
    assert not ev.number_matches(49, m.text)
    assert ev.number_matches(4.9, m.text)


@pytest.mark.parametrize("unit,expected", [
    ("MWh", "MWh"), ("megawatt hours", "MWh"), ("Megawatt-hours", "MWh"), ("MWh/yr", "MWh"),
    ("MWh per year", "MWh"), ("thousand MWh", "GWh"), ("MWh (thousands)", "GWh"), ("million kWh", "GWh"),
    ("gigajoules", "GJ"), ("tonnes", None),
])
def test_unit_spellings(unit, expected):
    assert canonical_unit("energy_consumption", unit) == expected


def test_currency_unit_spellings():
    assert canonical_unit("capex", "$ millions") == "USD_millions"
    assert canonical_unit("capex", "US$ billion") == "USD_billions"
    assert canonical_unit("capex", "millions of USD") == "USD_millions"


def test_unit_found_in_table_heading(src):
    m = ev.locate("Energy consumption 1,053,479 815,864 593,953", src)
    assert ev.unit_present("MWh", src.context(m, 300))       # "Energy (MWh)*" heads the section
    assert ev.unit_present("GWh", "1,053 thousand MWh", raw_unit="thousand MWh")
    assert not ev.unit_present("GWh", "1,053,479 MWh", raw_unit="GWh")


def _long_report() -> str:
    filler = ("NVIDIA Sustainability | Message From Our CEO Introduction Energy, Efficiency, and Climate Our People "
              "Product Value Chain Responsible Business Sustainability Indicators. We are committed to our employees, "
              "communities and partners, and to responsible business conduct across our value chain. ")
    boiler = ("Forward-looking statements in this report are subject to risks and uncertainties; NVIDIA disclaims any "
              "obligation to update these forward-looking statements. Laws and regulations may change. ")
    body = _norm_ws(" ".join(pages()))
    # the energy table sits deep in a ~110 kB report, after ~95 kB of prose and boilerplate
    return (filler * 280) + (boiler * 40) + (filler * 20) + " " + body + " " + (filler * 30)


def test_windows_reach_the_energy_table_deep_in_a_long_report():
    text = _long_report()
    assert len(text) > 100_000 and text.index("1,053,479") > 90_000
    w = ev.windows(text, 8000)
    assert len(w) <= 8000 + 10 * len(ev.GAP)
    assert "Energy consumption 1,053,479 815,864 593,953" in w and "Metric FY26 FY25 FY24" in w
    assert "trillion- parameter inference" in w or "Blackwell Ultra" in w     # qualitative share
    assert w.count("forward-looking statements") <= 2
    for part in w.split(ev.GAP):
        assert part in text                                # verbatim slices only


def test_budget_allocation_does_not_let_one_document_starve_the_rest():
    big = _long_report()
    table_heavy = " ".join(["Energy consumption 1,234,567 MWh electricity consumption 2,345 MWh."] * 3000)
    short = "NVIDIA launched a new platform and announced a gigawatt-scale AI factory. " * 40
    plans = {1: ev.plan(big), 2: ev.plan(table_heavy), 3: ev.plan(short)}
    budgets = ev.allocate(plans, {1: len(big), 2: len(table_heavy), 3: len(short)}, 30_000, 20_000)
    assert sum(budgets.values()) <= 30_000
    assert budgets[3] == len(short)                        # short sources are sent whole
    assert budgets[1] >= 8_000 and budgets[2] <= 20_000


def test_pdf_extraction_keeps_every_page_up_to_the_cap():
    from pypdf import PdfWriter
    import io
    w = PdfWriter()
    for _ in range(3):
        w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    _title, text = extract_text("application/pdf", buf.getvalue())
    assert text == ""          # blank pages: no crash, nothing invented
