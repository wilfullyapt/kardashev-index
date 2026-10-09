"""Bug 1 part 3: over-cap PDFs are read via HTTP Range or a streamed download, in a capped child."""
import re
import sys
from dataclasses import replace

import httpx
import pytest

from app.pipeline import largepdf as L
from app.pipeline import semantics as S
from app.pipeline.fetch import Assessment, FetchResult

PUBLIC = lambda host, port: [(2, 1, 6, "", ("93.184.216.34", 0))]
URL = "https://ir.example.com/impact-report.pdf"


def make_pdf(pages: list[str], pad_bytes: int = 0) -> bytes:
    """A small valid PDF: one Helvetica text line per page, plus an unused padding object so the
    file is big while the useful objects stay small (like image-heavy reports)."""
    objs: list[bytes] = []
    n = len(pages)
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    font_id = 3 + 2 * n
    for i, text in enumerate(pages):
        content = f"BT /F1 10 Tf 40 700 Td ({text}) Tj ET".encode()
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 {font_id} 0 R >> >> "
                    f"/Contents {4 + 2 * i} 0 R >>".encode())
        objs.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    if pad_bytes:
        objs.append(b"<< /Length %d >>\nstream\n" % pad_bytes + b"\x00" * pad_bytes + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def report(n_pages=60, energy_at=(51,), pad=8_000_000) -> bytes:
    pages = [f"Page {i + 1} narrative about our mission and products" for i in range(n_pages)]
    for p in energy_at:
        pages[p - 1] = "Total energy consumption 13,812,000 GJ in 2024 across operations"
    return make_pdf(pages, pad_bytes=pad)


def server(blob: bytes, *, ranges=True, log=None):
    def handler(request: httpx.Request) -> httpx.Response:
        rng = request.headers.get("range")
        if log is not None:
            log.append(rng)
        if ranges and rng:
            a, b = (int(x) for x in re.match(r"bytes=(\d+)-(\d+)", rng).groups())
            b = min(b, len(blob) - 1)
            return httpx.Response(206, content=blob[a:b + 1], headers={
                "content-range": f"bytes {a}-{b}/{len(blob)}", "content-type": "application/pdf"})
        return httpx.Response(200, content=blob, headers={"content-type": "application/pdf",
                                                          "content-length": str(len(blob))})
    return httpx.MockTransport(handler)


LIM = L.Limits(enabled=True)


def test_range_mode_reads_only_a_small_part_of_a_big_file():
    blob = report()
    res = L.read_in_child(URL, LIM, transport=server(blob), resolve=PUBLIC)
    assert res.ok and res.method == "range"
    assert res.pages_kept == [51]
    assert "[page 51]" in res.text and "13,812,000 GJ" in res.text
    assert res.total_bytes == len(blob)
    assert res.bytes_read < len(blob) / 4          # the 8 MB padding is never downloaded


def test_tail_pages_scanned_first_and_scan_cap_respected():
    blob = report(n_pages=100, energy_at=(5,), pad=0)
    res = L.read_in_child(URL, replace(LIM, scan_pages=40), transport=server(blob), resolve=PUBLIC)
    assert not res.ok and res.pages_scanned == 40 and "no page with an energy figure" in res.reason
    assert L.page_order(10, [2]) == [2, 9, 8, 7, 6, 0, 1, 3, 4, 5]


def test_keep_pages_and_char_caps():
    blob = report(n_pages=50, energy_at=tuple(range(1, 51)), pad=0)
    res = L.read_in_child(URL, replace(LIM, keep_pages=5), transport=server(blob), resolve=PUBLIC)
    assert res.ok and len(res.pages_kept) == 5
    res = L.read_in_child(URL, replace(LIM, max_chars=100), transport=server(blob), resolve=PUBLIC)
    assert len(res.text) <= 100


def test_range_budget_overrun_falls_back_to_streamed_download(tmp_path):
    blob = report(pad=0)
    res = L.read_in_child(URL, replace(LIM, range_budget_mb=0.0001), transport=server(blob),
                          resolve=PUBLIC, tmpdir=str(tmp_path))
    assert res.ok and res.method == "disk" and res.pages_kept == [51]
    assert list(tmp_path.iterdir()) == []          # temp file always removed


def test_no_range_support_streams_to_disk(tmp_path):
    blob = report(pad=1_000_000)
    res = L.read_in_child(URL, LIM, transport=server(blob, ranges=False), resolve=PUBLIC, tmpdir=str(tmp_path))
    assert res.ok and res.method == "disk" and "13,812,000 GJ" in res.text
    assert list(tmp_path.iterdir()) == []


def test_download_cap_refuses_and_cleans_up(tmp_path):
    blob = report(pad=3_000_000)
    srv = server(blob, ranges=False)
    lim = replace(LIM, max_download_mb=1)
    res = L.read_in_child(URL, lim, transport=srv, resolve=PUBLIC, tmpdir=str(tmp_path))
    assert not res.ok and "download cap" in res.reason        # Content-Length already over the cap
    # no Content-Length: the stream itself is cut at the cap
    def chunked(request):
        if request.headers.get("range"):
            return httpx.Response(200, content=b"%PDF-")
        return httpx.Response(200, content=iter([blob[i:i + 65536] for i in range(0, len(blob), 65536)]))
    res = L.read_in_child(URL, lim, transport=httpx.MockTransport(chunked), resolve=PUBLIC, tmpdir=str(tmp_path))
    assert not res.ok and "download cap" in res.reason
    assert list(tmp_path.iterdir()) == []


def test_low_disk_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(L.shutil, "disk_usage", lambda d: type("U", (), {"free": 10})())
    res = L.read_in_child(URL, LIM, transport=server(report(pad=0), ranges=False), resolve=PUBLIC,
                          tmpdir=str(tmp_path))
    assert not res.ok and "free disk" in res.reason


def test_ssrf_check_applies_to_redirects():
    def handler(request):
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest"})
    from app.pipeline.fetch import BlockedURL

    def resolve(host, port):
        ip = "169.254.169.254" if host.startswith("169.") else "93.184.216.34"
        return [(2, 1, 6, "", (ip, 0))]
    with pytest.raises(BlockedURL):
        L.read_in_child(URL, LIM, transport=httpx.MockTransport(handler), resolve=resolve)


def test_rangefile_cache_is_bounded():
    blob = bytes(range(256)) * 8192                 # 2 MB
    with httpx.Client(transport=server(blob)) as c:
        rf = L.RangeFile(c, URL, len(blob), budget=10_000_000, block=65536, cache_blocks=4, resolve=PUBLIC)
        assert rf.read(10) == blob[:10]
        rf.seek(-5, 2)
        assert rf.read() == blob[-5:]
        rf.seek(0)
        assert rf.read() == blob
        assert len(rf._cache) <= 4
        with pytest.raises(L.BudgetExceeded):
            L.RangeFile(c, URL, len(blob), budget=100, resolve=PUBLIC).read(1000)


# ------------------------------------------------------------- child process safeguards

def test_child_process_reads_a_local_file(tmp_path):
    f = tmp_path / "r.pdf"
    f.write_bytes(report(pad=0))
    res = L.run_child(["--file", str(f)], replace(LIM, timeout_s=60))
    assert res.ok and res.pages_kept == [51]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="RLIMIT_AS semantics")
def test_child_memory_cap_fails_safely(tmp_path):
    f = tmp_path / "r.pdf"
    f.write_bytes(report(pad=0))
    res = L.run_child(["--file", str(f)], replace(LIM, memory_mb=20, timeout_s=60))
    assert not res.ok and ("memory cap" in res.reason or "exited" in res.reason)


def test_child_timeout_is_enforced(monkeypatch):
    def slow(*a, **k):
        raise L.subprocess.TimeoutExpired(cmd="x", timeout=1)
    monkeypatch.setattr(L.subprocess, "run", slow)
    res = L.run_child(["--file", "x"], LIM)
    assert not res.ok and res.reason.startswith("time limit")


def test_only_one_large_pdf_at_a_time(monkeypatch):
    monkeypatch.setattr(L, "run_child", lambda args, limits: L.Result(ok=True, text="x"))
    assert L.read_large_pdf(URL, LIM).ok
    assert L._SLOT.acquire(blocking=False)
    try:
        res = L.read_large_pdf(URL, LIM)
        assert not res.ok and "another large PDF" in res.reason
    finally:
        L._SLOT.release()


def test_disabled_switch():
    res = L.read_large_pdf(URL, replace(LIM, enabled=False))
    assert not res.ok and "disabled" in res.reason


# ------------------------------------------------------------- fetch-stage integration

def _oversized():
    res = FetchResult(url=URL, final_url=URL, status=200, content_type="application/pdf",
                      body=b"%PDF-1.7 " + b"x" * 100)
    res.truncated = True
    return res, Assessment("error", "PDF larger than 30 MB cap (FETCH_MAX_BYTES); not parsed")


def test_rescue_turns_oversized_pdf_into_ok_source():
    res, a = _oversized()
    out = L.rescue_oversized(URL, res, a, lambda url: L.Result(
        ok=True, text="[page 51] Total energy consumption 13,812,000 GJ", method="range",
        total_bytes=129_000_000, bytes_read=3_000_000, pages_total=200, pages_scanned=40, pages_kept=[51]))
    assert out.status == "ok" and "13,812,000 GJ" in out.snapshot.text
    assert "HTTP Range" in out.reason and "129 MB" in out.reason
    assert res.body == b""


def test_rescue_failure_keeps_rejection_with_reason():
    res, a = _oversized()
    out = L.rescue_oversized(URL, res, a, lambda url: L.Result(ok=False, reason="memory cap (256 MB) reached"))
    assert out.status == "error" and "memory cap" in out.reason and "larger than" in out.reason


def test_rescue_ignores_other_errors():
    res, a = _oversized()
    res.truncated = False
    called = []
    assert L.rescue_oversized(URL, res, a, lambda url: called.append(url)) is a and not called


def test_pipeline_reads_oversized_report_through_injected_reader(db, monkeypatch):
    """End to end: the sustainability report is over the fetch cap; the large-PDF reader supplies
    its energy pages, the source is kept and the energy figure is measured."""
    from app import main
    from app.db import SessionLocal
    from app.models import Company, JudgmentRun, Source
    from app.worker import Worker
    from tests import fakes

    big = FetchResult(url=fakes.SUSTAIN_URL, final_url=fakes.SUSTAIN_URL, status=200,
                      content_type="application/pdf", body=b"%PDF-1.7 " + b"x" * 1000,
                      error="body larger than 30000000 bytes (truncated)")
    big.truncated = True
    calls = []

    def reader(url):
        calls.append(url)
        return L.Result(ok=True, method="range", total_bytes=129_000_000, bytes_read=2_500_000,
                        pages_total=180, pages_scanned=45, pages_kept=[150, 151], title="Impact Report",
                        text=f"[page 150] {fakes.ENERGY_SENTENCE} {fakes.DC_SENTENCE} [page 151] {fakes.PLANNED_SENTENCE}")
    deps = fakes.make_deps(fakes.world_llm(), routes=fakes.world_routes({fakes.SUSTAIN_URL: big}))
    deps = replace(deps, large_pdf=reader)
    monkeypatch.setattr(main, "worker", Worker(SessionLocal, lambda: deps))
    c = Company(canonical_name="nvidia", industry="Unknown")
    db.add(c)
    db.commit()
    run, _ = main.runs_svc.enqueue_run(db, c.id, trigger="rerun", triggered_by="test")
    main.worker.process_next()
    db.expire_all()
    run = db.get(JudgmentRun, run.id)
    src = db.query(Source).filter_by(run_id=run.id, url=fakes.SUSTAIN_URL).one()
    assert calls == [fakes.SUSTAIN_URL]
    assert src.status == "ok" and "HTTP Range" in src.reject_reason and fakes.ENERGY_SENTENCE[:40] in src.text
    assert run.status == "succeeded"
    from app.models import Metric
    used = {m.metric_key: m.source_id for m in db.query(Metric).filter_by(run_id=run.id)}
    assert src.id in used.values(), used


def test_fake_fetchers_never_spawn_the_reader():
    from app.pipeline.runner import Deps
    assert Deps.__dataclass_fields__["large_pdf"].default is None


def test_rate_limited_block_is_retried_once_then_download_error_is_reported(tmp_path):
    blob = report(pad=0)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if request.headers.get("range") == "bytes=0-1023":
            return httpx.Response(206, content=blob[:1024], headers={"content-range": f"bytes 0-1023/{len(blob)}"})
        return httpx.Response(429, headers={"retry-after": "0"})
    res = L.read_in_child(URL, LIM, transport=httpx.MockTransport(handler), resolve=PUBLIC, tmpdir=str(tmp_path))
    assert not res.ok and "HTTP 429" in res.reason
    assert calls["n"] == 4          # probe, block, one retry, download attempt
    assert list(tmp_path.iterdir()) == []


# ------------------------------------------------------------- unlabelled multi-value rows



TESLA_P195 = ("Uptime of Tesla Supercharger Sites* 99.95% Energy Consumption (kWh) 1,673,681,511 "
              "681,364,318 1,477,221,711 Impact Report 2024 Key Metrics")


def _check(quote, ctx=None, key="energy_consumption"):
    return S.check_figure(key, quote=quote, period="2024", is_primary=True, current_year=2026,
                          row_context=ctx if ctx is not None else quote)


@pytest.mark.parametrize("quote", ["Energy Consumption (kWh) 1,673,681,511",
                                   "Energy Consumption (kWh) 1,673,681,511 681,364,318 1,477,221,711"])
def test_value_among_unlabelled_values_is_ambiguous(quote):
    v = _check(quote, TESLA_P195)
    assert not v.ok and v.reason.startswith("ambiguous: 3 unlabelled values")
    assert "1,477,221,711" in v.reason


@pytest.mark.parametrize("quote,ctx", [
    ("Total energy consumption (kWh) 3,832,267,540", None),                       # source states a total
    ("Energy consumption (MWh) 2024 2023 612,000 489,600", None),                 # year columns label them
    ("Energy consumption (GJ) FY2024 FY2023 13,796,163 9,285,005", None),
    ("Energy consumption (MWh) 2024 612,000", "Energy consumption (MWh) 2024 612,000 Water (m3) 2024 5,000"),
    ("In fiscal year 2024, total energy consumption across our operations was 612,000 MWh.", None),
])
def test_labelled_or_total_figures_still_pass(quote, ctx):
    assert _check(quote, ctx).ok


def test_unlabelled_rule_only_for_scored_energy():
    v = _check("Energy storage deployed (MWh) 31,400 14,700", key="energy_storage_deployed")
    assert v.ok
