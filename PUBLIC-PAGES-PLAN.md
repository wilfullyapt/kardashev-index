# Public Pages Plan — Kardashev Index (public-pages branch)

**Goal:** Ship a clean, fast public frontend (SSR + Jinja2 + HTMX + Tailwind) on top of the existing scaffold.

## Stack
- FastAPI + Jinja2 (existing)
- Tailwind CSS (CDN for speed)
- HTMX (for search, forms, partial updates)
- Minimal custom JS

## Phases

### Phase 1 — Foundation (current)
- [ ] Add HTMX + Tailwind CDN includes
- [ ] Create `templates/base.html` (nav, footer, flash messages)
- [ ] Uncomment StaticFiles mount
- [ ] Add `/` route → public leaderboard

### Phase 2 — Core Public Pages
- [ ] `GET /` → ranked leaderboard table (searchable via HTMX)
- [ ] `GET /companies/{id}` → company detail + scores + history
- [ ] `GET/POST /suggest` → public suggestion form (reuse existing logic)

### Phase 3 — Polish & Deploy
- [ ] Basic responsive styling
- [ ] Error/empty states
- [ ] Test locally + push to Render
- [ ] Squash or PR back to main when stable

**Success criteria:** Public site shows live leaderboard and company pages with no 404s on root. Admin flows remain untouched.