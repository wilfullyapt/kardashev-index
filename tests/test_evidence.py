from app.pipeline import evidence as ev

SRC = "In fiscal 2024 our total energy consumption was 612,000\u00a0MWh \u2014 the \u201cgreenest\u201d year yet. Data centers: 48 MW."


def test_verbatim_quotes_tolerate_whitespace_case_and_typography():
    n = ev.normalize(SRC)
    assert ev.find_quote("total energy consumption was 612,000 MWh", n) >= 0
    assert ev.find_quote('612,000 MWh - the "greenest" year yet', n) >= 0
    assert ev.find_quote("TOTAL ENERGY   CONSUMPTION was 612,000 mwh", n) >= 0


def test_paraphrases_and_trivial_quotes_are_rejected():
    n = ev.normalize(SRC)
    assert ev.find_quote("total energy use was 612,000 MWh", n) == -1
    assert ev.find_quote("48 MW", n) == -1                       # too short to be meaningful
    assert ev.find_quote("x" * 700, n) == -1


def test_number_matching():
    assert ev.number_matches(612000, "consumption was 612,000 MWh")
    assert ev.number_matches(4.2e9, "capex of $4.2 billion")
    assert ev.number_matches(48, "a combined 48 MW")
    assert ev.number_matches(1234567.5, "1 234 567.5 GJ")
    assert not ev.number_matches(700000, "consumption was 612,000 MWh")


def test_unit_presence():
    assert ev.unit_present("MWh", "612,000 MWh")
    assert ev.unit_present("GWh", "5 gigawatt hours")
    assert not ev.unit_present("MWh", "612,000 GWh")
    assert ev.unit_present("USD_billions", "$4.2 billion")


def test_windows_are_verbatim_slices_around_keywords():
    text = ("Lorem ipsum dolor sit amet. " * 400) + "Total electricity use was 2.1 TWh in 2024. " + ("Filler text. " * 400)
    w = ev.windows(text, 2000)
    assert "2.1 TWh" in w and len(w) <= 2010
    for part in w.split(ev.GAP):
        assert part in text
    assert ev.windows("short", 100) == "short"
