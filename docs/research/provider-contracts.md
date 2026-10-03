# Provider Contracts for Phase 1

**Status:** implementation contract; every live adapter must pass preflight before run registration.

| Capability | Primary contract | Authentication | Required fields / timestamp | Fallback and registration rule |
|---|---|---|---|---|
| OpenAI evidence model | `POST https://api.openai.com/v1/responses`; strict JSON schema; tools omitted | `OPENAI_API_KEY` Bearer | requested/returned model, response ID, usage, raw bytes, provider metadata, completed timestamp | No model fallback. Exact snapshot and comparable fingerprint must pass preflight. |
| Jev | `POST https://api.typesafe.ai/v1/systemone`; request `{model,state,questions}` | `TYPESAFE_API_KEY` Bearer | immutable model ID/revision, answers, probabilities, confidence, usage/cost, latency, raw bytes | No OpenRouter fallback in registered run. Missing key or stable comparable revision blocks registration. |
| Polymarket catalog | Gamma API `https://gamma-api.polymarket.com/markets` read-only GET | none | immutable condition/market/token IDs, categories/tags, question, rules, resolution source/end, active/closed, volume/liquidity; first retrieval time | No silent provider fallback. Fixture contract drift blocks the cycle. |
| Polymarket books | CLOB API `https://clob.polymarket.com/book?token_id=...` read-only GET | none | token ID, bids/asks price and integer size, server/client retrieval time, raw hash | Missing/stale book means no trade/exit fill; conservative mark policy applies. |
| Polymarket resolution | Gamma market status plus stated resolution-source document | none | official outcome/payout, close/resolution timestamp, source document hash | Disputed/unavailable outcome remains unresolved at fixed T_m. |
| Stock OHLCV/actions | Yahoo Finance chart v8 `https://query1.finance.yahoo.com/v8/finance/chart/{symbol}` with `interval=1d&events=div,splits` | none | raw OHLCV, dividends, splits, exchange timezone, response retrieval time | This is an unofficial public endpoint. Preflight and terms review are registration gates. No replacement after registration. |
| S&P 100 membership | Frozen Wikipedia `S%26P_100` table artifact plus source/retrieval hash | none | symbol, company, frozen retrieval time | Phase 1 is forward-only; membership stays frozen. Retrieval/schema failure blocks registration. |
| Treasury cash yield | FRED graph CSV `https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS3MO` | none | observation date/value and first retrieval time | Last published prior observation may be used only under manifest freshness rule; no backdating revisions. |
| SEC evidence | `https://data.sec.gov/submissions/CIK##########.json`, filing archives, and XBRL companyfacts | descriptive `User-Agent` with contact | accession, filed/accepted timestamps, form, primary document, facts, first retrieval time | SEC rate limit/backoff frozen; amendments append and never overwrite. |
| Issuer/news evidence discovery | Company investor-relations RSS/Atom when available; Google News RSS query only as a discovery index, followed by safe retrieval of the original source | none | feed item publication time, original URL, first retrieval time, exact stored bytes | Allowed source classes/domains frozen in manifest; no model-side browsing. Missing evidence causes abstention/validation failure, never hidden fallback. |
| NYSE calendar | pinned `exchange-calendars` package/version | none | sessions, holidays, early closes, America/New_York to UTC | Calendar package/version hash is part of manifest/runtime identity. |
| Stock spread model | Frozen Phase-1 assumption: half-spread = 5 bps for S&P 100/OEF; extra adverse slippage = 5 bps/side | none | model version and constants | Replayed at 0.5x/1.0x/1.5x uncertain costs. No observed quote substitution mid-run. |
| Borrow/locate | none approved | n/a | n/a | Phase 1 stocks are **long/cash only**. Shorts remain disabled in AI and deterministic arms. |

## Common observation envelope

Every provider record uses:

```python
class ObservationMetadata(BaseModel, frozen=True):
    source_uri: str
    published_at_us: int | None
    retrieved_at_us: int       # first successful retrieval
    status: Literal["ok", "missing", "invalid", "stale"]
    content_sha256: str
    supersedes_id: str | None = None
```

A later retrieval cannot alter `retrieved_at_us` or overwrite content. Corrections append a new observation linked through a validated acyclic supersession chain.

## Licensing and cost treatment

- Public HTTP access does not imply redistribution rights. Raw third-party payloads remain private experiment artifacts and are not committed.
- Provider terms and live accessibility are checked by preflight and recorded.
- Actual model/data charges debit the AI account under the approved allocation policy.
- If Yahoo, TypeSafe, or any required contract fails preflight, registration is blocked; implementation does not invent a replacement.
