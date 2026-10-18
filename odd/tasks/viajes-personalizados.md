# Feature: viajes personalizados por Telegram

## Objective
Let the user create and manage flight searches by chatting with the Telegram bot
(destination, origin, start date, flexibility, trip length, travellers, budget), instead of
having a single trip hard-coded in `buscador_vuelos.py`.

## Problem / why
Today one trip (AQP -> LIM -> MAD, 3 adults) lives in CONFIG. Each new trip needs a code edit.
A bot that answers instantly cannot run on GitHub Actions (polling lag of 5-20 min), so the
chat front-end is a Cloudflare Worker (free plan) and the search stays on GitHub Actions.

## Architecture
- `viajes.json` (repo root): list of trips. Written by the Worker through the GitHub
  Contents API; read by the search.
- `buscador_vuelos.py`: runs every active trip; per-trip state in `estado_viajes.json`.
- `worker/`: Cloudflare Worker (JS). Conversation state in KV. Webhook secret header check,
  only the owner's chat id is obeyed. Destination text -> IATA via the public Travelpayouts
  autocomplete endpoint.
- Workflow also runs on push to `viajes.json`, so a new trip is searched right away.

## Scope / constraints
- Free only. No paid APIs. Google Flights stays the live source (unofficial).
- Authorised: edit this repo, create/deploy the Worker in the user's Cloudflare account,
  set Worker secrets from `.env` (never print them), commit to `main` of the user's repo.
- Not authorised: anything outside this repo and the Cloudflare Worker `agente-vuelos-bot`.
- TDD: not configured for this project (no test runner in repo); functional checks are
  ad-hoc Python scripts run with the scratch venv. Mode: off. Source: project state.

## Tasks
- [x] T1 Generalise Python to run N trips from `viajes.json` (route: inline, 1 file + data)
  Evidence: 21 ad-hoc checks pass (multi-trip, per-trip state, BOM, no-hub trips, failure
  isolation); real Actions run reproduced 2.715 EUR / 3 adults. Commit c7a703c.
- [x] T2 Initial `viajes.json` migrated from the current CONFIG; per-trip state; workflow update
  Evidence: push to `viajes.json` triggered the workflow by itself (event=push), seeded state
  prevented a duplicate alert. Commit c7a703c.
- [x] T3 Cloudflare Worker: conversation, validation, IATA lookup, GitHub write (route: inline)
  Evidence: `node worker/test.mjs` 43/43 (full conversation, 409 retry, webhook secret, foreign
  chat ignored, delete). Commit 5b1286c.
- [x] T4 Deploy Worker with wrangler, set secrets, register Telegram webhook
  Evidence: deployed `agente-vuelos-bot` (4 secrets uploaded, KV bound, preview URLs off);
  Telegram `setWebhook` ok with secret_token and allowed_updates=["message"]; live Worker
  answers 200 to GET and 403 to POST without/with a wrong webhook secret. wrangler OAuth
  login failed ("Unauthorized"), so a Cloudflare API token from `.env` was used instead.
- [ ] T5 End-to-end test: create a trip from Telegram, see it searched and alerted
  Status (2026-10-18): `/viajes` verified live from Telegram. `/viaje` creation failed once because the
  GitHub token was read-only; permission fixed (write test: PUT with a wrong sha now returns 409, not 403).
  Still to do by the user: run `/viaje` with a cheap test trip, confirm "Guardado", then `/borrar 2`.
- [x] T6 Docs (LEEME) and cleanup
  Evidence: LEEME describes viajes.json, the bot and its commands; Python tests moved into
  `tests/test_viajes.py` (21 checks pass).

## Acceptance criteria
- `/viaje` in Telegram walks through the questions and stores a valid trip in `viajes.json`.
- Messages from any other chat are ignored.
- A new trip triggers a search and an alert with the purchase guide.
- The existing Madrid trip keeps working with the same results.

## Progress
Design agreed with user: Telegram chat via Cloudflare Worker. Worker `agente-vuelos-bot`
already created in the user's account (`agente-vuelos-bot.marcelinaloyola0.workers.dev`).
GitHub fine-grained token pending to be saved in `.env` as GITHUB_TOKEN.

## Next step
T5: the owner runs `/viaje` once from Telegram. Everything else in this feature is done. The follow-up product
work (Kuntur) is tracked separately as the SDD change `kuntur-platform`; see HANDOFF.md.
