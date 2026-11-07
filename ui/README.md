# UI (secondary — the loop is the product)

Three views, read from the store:

1. **Reasoning map** — UMAP 2D projection of `steps` embeddings; verified runs in grey, failed runs in red, failure regions as circles, the live-warned step highlighted.
2. **Changelog** — every `Change` with status, held-out before/after and reason.
3. **Trust leaderboard** — `trust` collection, score over time.

Plan: a single `index.html` + a tiny FastAPI endpoint (`ui/server.py`) that dumps the three collections as JSON. Build only after the loop works end to end.
