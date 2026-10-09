# Wayback Machine fixtures (recorded 2026-10-09 from the box, public endpoints, no auth)

| File | What it is |
|---|---|
| `cdx_tesla_impact.json` | `web.archive.org/cdx/search/cdx?url=https://www.tesla.com/impact&output=json&fl=timestamp,original,mimetype,statuscode,length&filter=statuscode:200&limit=-3` (verbatim) |
| `cdx_tesla_extended_pdf.json` | same query for `www.tesla.com/ns_videos/2024-extended-version-tesla-impact-report.pdf` (verbatim) |
| `cdx_empty.json` | CDX answer when there is no matching capture (`[]`) |
| `availability_empty.json` | `archive.org/wayback/available?url=https://www.tesla.com/impact`: empty although CDX lists 200 captures (verbatim) |
| `availability_ok.json` | documented success shape of the availability API (live calls were rate-limited while recording) |
| `web_archive_available_404.html` | `web.archive.org/wayback/available?...` answers HTTP 404 with this body (verbatim) |
| `ia_429.html` | archive.org rate-limit page, HTTP 429 (verbatim) |
| `ia_temporarily_offline.html` | "Temporarily Offline" page served with HTTP 200 by CDX (verbatim, base64 logos removed) |
| `akamai_access_denied.html` | the 2026-01-11 capture of tesla.com/impact: Akamai's 403 page archived as-is (verbatim) |
| `tesla_impact_20260611033023.html` | raw (`id_`) capture of tesla.com/impact, 2026-06-11; `<script>`/`<style>`/`<svg>` bodies and data: URIs stripped to keep it small, links and text unchanged |

Tesla page content is third-party material kept only as a test fixture.
