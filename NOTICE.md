# Notices and third-party material

Kardashev Index. Copyright (c) 2026 Wil Mawhinney.

- **Code:** MIT. See [`LICENSE`](LICENSE).
- **Scores, data and methodology text:** CC BY 4.0. See [`DATA-LICENSE.md`](DATA-LICENSE.md).

## Third-party assets used by the site

| Asset | How it is used | License |
|---|---|---|
| [htmx](https://htmx.org/) 1.9.12 | Vendored at `static/vendor/htmx-1.9.12.min.js` (unmodified; SRI-pinned) | Zero-Clause BSD (0BSD), Big Sky Software. [License at v1.9.12](https://github.com/bigskysoftware/htmx/blob/v1.9.12/LICENSE). htmx moved from BSD 2-Clause to 0BSD during 1.9.x; 1.9.12 is 0BSD |
| Cinzel (Natanael Gama) | Loaded from Google Fonts (not redistributed) | SIL Open Font License 1.1 |
| Cormorant Garamond (Christian Thalmann) | Loaded from Google Fonts (not redistributed) | SIL Open Font License 1.1 |
| Inter Tight (Rasmus Andersson et al.) | Loaded from Google Fonts (not redistributed) | SIL Open Font License 1.1 |
| JetBrains Mono (JetBrains) | Loaded from Google Fonts (not redistributed) | SIL Open Font License 1.1 |

The OFL 1.1 text is at <https://openfontlicense.org/open-font-license-official-text/>.
The fonts are served by Google, so no font files ship in this repository.

### htmx license (0BSD, as published with v1.9.12)

0BSD does not require attribution. The text is reproduced here for completeness.

```
Zero-Clause BSD
=============

Permission to use, copy, modify, and/or distribute this software for
any purpose with or without fee is hereby granted.

THE SOFTWARE IS PROVIDED “AS IS” AND THE AUTHOR DISCLAIMS ALL
WARRANTIES WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES
OF MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE
FOR ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY
DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN
AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT
OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
```

## Project images (`static/img/`)

`favicon.svg`, `mark.svg`, `hero-star.svg`, `grain.svg`, `favicon-32.png`, `apple-touch-icon.png`
and `og.jpg` were added in commit 238aa71 as "original procedural art" made for this project. The
SVGs are hand-written vector code (gradients and shapes, no embedded raster or third-party art).
They are covered by the MIT License with the rest of the repository.

**Provenance to confirm:** the raster files (`og.jpg`, `favicon-32.png`, `apple-touch-icon.png`)
carry no metadata saying how they were made. They appear to be renders of the project's own SVGs
and type, possibly AI- or tool-generated. If any was produced with a generator whose terms restrict
reuse, or contains third-party imagery, this section must be updated. Any text drawn in `og.jpg`
that uses the OFL fonts above is fine to distribute as an image.

## Dependencies (not redistributed)

Python packages in `requirements*.txt` (FastAPI, Starlette, SQLAlchemy, Alembic, Jinja2, httpx, pypdf,
etc.) are installed at build time, not vendored. Each keeps its own license.
