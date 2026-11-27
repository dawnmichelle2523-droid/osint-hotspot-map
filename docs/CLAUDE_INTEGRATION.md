# Claude integration (on demand)

Claude is optional. The map, the pollers, the 07:00 briefings, and the scheduled reports keep working with no Anthropic key and no Anthropic spend. A draft is sent to Claude only when someone clicks **Draft with Claude** (or calls the draft endpoint) and a server key is set.

Nothing in this path calls xAI.

## Environment

Set these on the server process. Do not put them in HTML or JavaScript, and do not commit a real key.

| Variable | Purpose | Default |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | Preferred server key for the Messages API. | unset |
| `GeoRiskGlobal` | Fallback key, used only when `ANTHROPIC_API_KEY` is unset or blank. The name is case-sensitive. `GEORISKGLOBAL` is not read. | unset |
| `GeoRiskIntel` | Legacy fallback, used only when both names above are unset or blank. Kept so the existing Vercel Production secret still works. The name is case-sensitive. `GEORISKINTEL` is not read. | unset |
| `CLAUDE_RA_MODEL` | Model for country and sector risk-assessment drafts. | `claude-sonnet-5-5` |
| `CLAUDE_BRIEF_MODEL` | Model for daily, weekly, and monthly area briefings. | `claude-haiku-5-5` |

Claude stays off when all three key values are unset or blank. The module reads the process environment. A local `.env` file is loaded only when `python-dotenv` is installed, and it does not override variables that are already set. The key is never written into a response or a log line.

## Endpoints

These routes do not run on a timer. `GET /api/ai/claude-status` only reads the environment. It does not call Anthropic.

| Method | Path | What it does |
| --- | --- | --- |
| `GET` or `POST` | `/api/ai/claude-status` | `{configured, ra_model, brief_model}` plus list-price notes. No key. |
| `POST` | `/api/risk-assessments/ai-draft` | One Sonnet draft, or the local template. |
| `POST` | `/api/briefings/ai-draft` | One Haiku draft for `daily`, `weekly`, `monthly`, or `custom`. |

Send JSON. Useful fields are `country`, `region`, `sector`, `deployment_scope` (or `operation`), `cadence`, `compliance_standard`, `hazards` (`threat`, `likelihood`, `impact`), and `incidents` (public title, summary, severity, category, place, time, source name). `action: "refine"` plus `draft` asks Claude to revise an earlier public draft.

The buttons live on `/risk-assessments`, `/briefing`, and `/reports`. The reports button does not send mail and is not part of the 07:00 schedule.

## What is sent

Drafts and the local template are written in UK English (British spelling, day-month-year dates, and terms such as organisation, traveller, prioritise, and medevac).

A risk draft is written as a security-director assessment. It uses the 5×5 Likelihood × Impact scale (Low 1–4, Moderate 5–9, High 10–14, Very High 15–19, Severe 20–25) and aligns the wording with ISO 31000, ISO 31030, and ISO 45001, plus BS 8848, ISO 18788, or ISO 22361 when the sector calls for that frame. It asks for threat-actor capability and intent only from the public context, route and checkpoint notes, NOTAM and EASA CZIB context, medevac and golden-hour constraints, the 2-tap check-in ladder (+30 / +60 / +120 minutes), shelter-in-place and hibernation triggers, and Go / Conditional-Go / No-Go criteria.

A briefing draft uses six headings, in order: BLUF, Verified Security Incidents, Sub-Regional/Governorate Status, Aviation/Routes/Infrastructure, 24h Operational Advice, and 24–72h Outlook.

The prompt is rebuilt on the server from a short allowlist:

- country, region, sector, and deployment scope
- the compliance frame on a risk draft
- hazard labels and scores
- public incident context (title, summary, severity, category, place, time, source name)

Emails, phone numbers, IMEIs, passport-like IDs, and labeled staff names are stripped first. Traveller names, contacts, medical notes, reviewer names, callsigns, and similar fields are dropped even if a client sends them. The browser scripts do not call `api.anthropic.com`. The key stays on the server.

## When Claude is not called

- `ANTHROPIC_API_KEY`, `GeoRiskGlobal`, and legacy `GeoRiskIntel` are all missing or blank
- Anthropic returns an error, a rate limit, or an empty draft
- any page load, health check, ingest poll, or scheduled report

The response is still a finished draft: the local template, with `source: "local"`, a plain-English `status`, and `usage.estimated_usd` of `0`. The list-price ceiling in `estimate` is what a call would have cost. It is not a bill. Published rates used for that ceiling are Sonnet 5.5 at $2 / $10 per million input / output tokens, and Haiku 5.5 at $0.10 / $0.50 per million tokens for prompts up to 100,000 tokens ($0.50 / $2.50 above that).

Each click makes at most one Messages API request. There is no retry. Risk drafts use effort `medium` and an output cap of 2400 tokens. Briefings use effort `low` and a cap of 1200 tokens. Thinking tokens, when the API bills them, count toward that cap and toward the output price.
