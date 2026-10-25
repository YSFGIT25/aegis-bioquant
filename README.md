# FDAOdds

Formerly Aegis-BioQuant. The code folder (`aegis_bioquant`), this
repository and the Railway project keep the original name on purpose:
the pre-registered study (https://osf.io/e6rcu) cites them, and renaming
the package would change the rules fingerprint (`config/rules.py`).

Scans upcoming FDA decisions (PDUFA dates, advisory committees; Phase 3
readouts as a watch list), scores each drug's evidence, reads what the
options market expects, and alerts on setups. No trades are placed: the
positions it prints are paper positions.

Dual-engine architecture:
- **Engine 1 — Evidence score**: an AI model's reading of the company's filings and published results
  (`engines/clinical_nlp/llm_review.py`), with a registry point system as the fallback (`classifier.py`) -> `P_Fundamental`
- **Engine 2 — Options Quant**: IV Rank + the move the options price for the event

The two engines feed the **Arbitrage Matrix** (`logic/matrix_matcher.py`), which
routes each candidate to one of three tracks:

| Signal | Condition | Execution |
| :--- | :--- | :--- |
| `LONG_RUN_UP_ALPHA` | P_Fundamental ≥ 0.75, options price an extra move ≥ 10% for the event, IV Rank < 30 | Short-dated options run-up, Kelly-sized, T-14/T-5 exits |
| `STRUCTURAL_COLLAPSE_AVOID` | P_Fundamental ≤ 0.40, extra move ≥ 10%, IV Rank > 50 | Short/avoid existing premium exposure |
| `LONG_TERM_EQUITY_ACCUMULATION` | P_Fundamental ≥ 0.90 (review not thin), but IV Rank > 50 or OI < 2000 (options overpriced/illiquid) | Bypass options, flat 2–5% equity allocation, 15% trailing stop, no time-based exit |

## Running it

Run both from this folder, so they share the same `aegis_bioquant.db` file.

Dashboard (leave it running, then open http://127.0.0.1:8765):

```powershell
python -m aegis_bioquant.telemetry.dashboard
```

Scan (its results and alerts appear on the dashboard by themselves — both
read and write the same database file):

```powershell
$env:SEC_EDGAR_CONTACT_EMAIL = "you@example.com"
python -m aegis_bioquant.pipeline
```

Telegram and Discord alerts are optional — see `aegis_bioquant/config/settings.py`.

## Hosted on Railway

Railway project `aegis-bioquant`, three services:

| Service | What it is |
| :--- | :--- |
| `Postgres` | The database. No public address; only the other two services can reach it. |
| `scanner` | Runs the scan once a day at 19:00 UTC (late in the US market session, when option quotes are live and most of the day's volume is in) and keeps its own schedule. |
| `web` | The customer site: https://web-production-70aa9.up.railway.app |

`scanner` and `web` are built from the same `Dockerfile`. The
`AEGIS_ROLE` variable set on each service (`scanner` or `web`) decides
what the container does — see `aegis_bioquant/entrypoint.py`.

```powershell
python deploy.py web                # deploy the current code to one service
python deploy.py scanner
railway logs --service scanner      # read a service's latest output
```

Use `deploy.py` rather than `railway up` directly: it names the project
explicitly, so a deploy can never land in a newly created project.

A scan that could not reach SEC EDGAR or the FDA calendar is retried once
after 30 minutes; the reason is listed under `INGEST` in its log.

Email verification and password reset switch on once the `web` service
has `RESEND_API_KEY` and `EMAIL_FROM` set (`PUBLIC_BASE_URL` already is)
— see `aegis_bioquant/config/settings.py`. Until then the site runs
without them.

The `/review` page (confirming which trial a catalyst is about) is only
for accounts listed in the `web` service's `ADMIN_EMAILS` variable.
Create the account on the site first, then list its email: a listed
address can no longer be signed up.

Outcome tracking: each scan looks in companies' SEC filings for the FDA's
decision on catalysts at or near their date and stores what it finds as a
suggestion. An administrator confirms it at `/review/outcomes`; only
confirmed outcomes appear in the dashboard's track record.

To run the customer site locally instead:

```powershell
$env:SESSION_COOKIE_SECURE = "0"
uvicorn aegis_bioquant.web.app:app --port 8000
```

See `aegis_bioquant/` for the full module layout. Build order and status for
each module are tracked inline (module docstrings marked `STATUS: STUB`).
