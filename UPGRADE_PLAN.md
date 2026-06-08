# BoxBox F1 Bot — Upgrade Plan & Implementation Roadmap

_Senior engineering audit + phased, agent-executable roadmap. No rewrite. Incremental, stack-preserving._
_Stack: python-telegram-bot 21.6, FastF1 3.4, Tavily, Groq (Llama 3.1/3.3 + Whisper + Orpheus TTS), APScheduler, edge-tts. Single long-lived polling worker (`Procfile: worker: python main.py`)._

---

## 1. Executive summary

**What the bot is today.** A genuinely good single-process Telegram F1 assistant with a wide command surface (`/race`, `/predict`, `/strategy`, `/fantasy`, `/standings`, `/rumour`, `/ask`, `/lap`, `/h2h`, `/history`, `/career`, `/rewind`, `/result`, `/notify`, voice notes). Architecture is clean and modular: thin handlers, data access in `utils/f1_data.py`, LLM in `utils/groq_client.py`, search in `utils/tavily_client.py`. Good instincts are already present — lazy clients, bounded caches, a safe Telegram sender with markdown fallback, rate limiting, `asyncio.to_thread` for blocking FastF1 work in most places.

**What it should become.** The best F1 super-fan bot on Telegram: reliable (no broken commands, survives restarts), fast (cached data, no redundant searches), interactive (inline menus, a race-weekend hub), and richer on race weekends (profiles, circuit pages, post-session reactions, personalized alerts). All of this is reachable **incrementally** on the current stack — no rewrite is warranted.

**Top priorities (in order).**
1. **Fix the two broken commands.** `/predict` and `/fantasy` crash every time because they wrap the `async` `get_next_race_info()` in `asyncio.to_thread(...)`, producing an un-awaited coroutine that is then subscripted (`next_race["name"]`). This raises `TypeError` and the user gets "Something went wrong."
2. **Stop losing state on restart.** `/notify` subscribers and the breaking-news dedup set live only in memory; every redeploy silently unsubscribes everyone.
3. **De-risk the data layer.** Caching + a shared HTTP client in front of Jolpi/Ergast (a community mirror of a now-deprecated API) to avoid rate-limit failures and cut latency.

After those three, layer in interactivity (inline menus / race-weekend hub) and the high-value feature set.

---

## 2. Current-state audit

### 2.1 Architecture issues
- **Async/sync boundary is inconsistent and has produced a real bug.** `get_next_race_info()` is `async` (it awaits aiohttp), but `predict.py:19-22` and `fantasy.py:19-22` call it via `asyncio.to_thread(get_next_race_info)`. `to_thread` runs the *coroutine function* in a worker thread, which just returns an un-awaited coroutine — never the data. See 2.2 #1. The mixed convention (some data fns are sync FastF1, some are async aiohttp) is the root cause; it should be made explicit.
- **No shared aiohttp session.** Every Jolpi/Telegram-file call opens and tears down its own `aiohttp.ClientSession()` (e.g. `f1_data.py` repeated ~6×, `history.py`, `voice.py`). No connection pooling; more overhead and more sockets than needed.
- **No caching on most data calls.** Only `get_next_race_info()` has a TTL cache (`f1_data.py:59-61`). `/standings`, `/result`, `/career`, `/history`, qualifying lookups all hit Jolpi fresh on every invocation, even though standings/results only change a few times a week.
- **Two near-duplicate result fetchers.** `get_last_race_results_async` (top-10) and `get_full_race_results_async` (all drivers) in `f1_data.py` differ only in slicing; the sync FastF1 `get_last_race_results` / `get_next_event` are largely superseded. `get_next_event` is **dead code** (no callers).
- **Reminder scheduling is boot-time only.** `_schedule_all_reminders` runs once in `post_init` (`notify.py:51-67`) against the current season's schedule. A process that lives across a season rollover (started in Dec) keeps last year's schedule until redeployed. APScheduler uses the in-memory job store, so the schedule is the source of truth — acceptable, but it never refreshes.
- **No persistence layer at all.** Subscribers, seen-news hashes, rate-limit windows are all in-process dicts/sets.
- **No tests.** Zero test files for a codebase full of brittle data-parsing branches.

### 2.2 Bugs / failure modes (severity tagged)
1. **[CRITICAL] `/predict` and `/fantasy` are broken.** `asyncio.to_thread(get_next_race_info)` returns a coroutine, not a dict. `predict.py` then does `race_name = next_race["name"]` (`predict.py:27`) and `fantasy.py` does `race_name = next_race["name"] if next_race else ...` (`fantasy.py:23`) → `TypeError: 'coroutine' object is not subscriptable` + a "coroutine was never awaited" warning. Both commands always fail.
2. **[HIGH] `/notify` state is lost on every restart.** `_subscribers` and `_seen_news_hashes` (`notify.py:20-24`) are in-memory. After any redeploy, users who opted in stop receiving reminders/news with no signal. On a worker dyno this happens often.
3. **[MEDIUM] `/ask` over-triggers live web search.** `LIVE_KEYWORDS` (`ask.py:28-34`) includes extremely common words — `driver`, `team`, `best`, `worst`, `pick`, `season`. Almost every question fires a Tavily "advanced" search (cost + latency), even pure-history questions ("who's the best driver ever"). `STANDINGS_KEYWORDS` similarly pulls *current* standings for questions about past seasons ("who won the 2008 title").
4. **[MEDIUM] Qualifying-round detection breaks on sprint weekends.** `get_qualifying_results` (`f1_data.py:213-263`) uses `Session4Date` as "qualifying". On sprint weekends the session ordering differs, so this can resolve the wrong session/round.
5. **[LOW] Dead code that misleads.** `NEWS_SOURCES` (`notify.py:40-48`) is never used — actual domain filtering is `ALLOWED_DOMAINS` in `tavily_client.py`. `get_next_event` in `f1_data.py` has no callers. Both invite incorrect assumptions during future edits.
6. **[LOW] `get_race_rewind_data` track-status laps are always "?".** `session.track_status` has no `LapNumber` column, so `row.get('LapNumber', '?')` (`f1_data.py:428`) always prints `Lap ~?`.
7. **[LOW] Raw exception strings leak to users.** Handlers surface `{"error": str(e)}` directly (e.g. `strategy.py:23-25`, `lap.py:104-106`), showing internal messages like `KeyError: 'Position'`.
8. **[LOW] `testvoice` debug command is publicly registered** (`main.py:129`) but hidden from the menu — leftover debugging, callable by anyone.
9. **[LOW] `result.py` column padding is invisible.** `f"P{pos:<2} ..."` (`result.py:47`) relies on monospace, but Telegram renders proportional unless wrapped in a code block.

### 2.3 UX issues
- **No interactivity.** Every command is one-shot text; no inline buttons, menus, or follow-ups. The prompt's "hub / interactive flows / pinned dashboards" goals have no foundation yet.
- **Markdown fragility.** Most outbound text uses legacy `parse_mode="Markdown"`. `safe_reply` retries as plain text on `BadRequest` (good), but `start_handler` (`main.py:78`), session reminders and breaking-news pushes (`notify.py:113-115, 197-200`) call `reply_text`/`send_message` with Markdown directly and bypass that safety net. When fallback triggers, `*bold*` markers show as literal asterisks (degraded look).
- **Inconsistent "data unavailable" behaviour.** Some handlers give friendly fallbacks (`race.py`, `rewind.py` search fallback), others dump raw errors (2.2 #7).
- **No discoverability of arguments.** `/lap`, `/h2h`, `/history`, `/rewind` require precise args; only show usage on empty input. No guided/inline entry.

### 2.4 Data-reliability issues
- **[STRATEGIC] Heavy dependence on Ergast via the Jolpi mirror.** Standings, results, career, history, round resolution all hit `api.jolpi.ca/ergast`. The original Ergast API is deprecated; Jolpi is a community mirror with real rate limits (HTTP 429 under burst). `/career` for a veteran pages through *all* results 100 at a time (`history.py:184-226`) — several sequential calls per command, uncached — a prime 429 trigger. This is the single biggest data risk.
- **Model-id drift.** Groq rotates/retires models. `llama-3.1-8b-instant`, `llama-3.3-70b-versatile`, `whisper-large-v3-turbo`, and `canopylabs/orpheus-v1-english` are hardcoded in `groq_client.py`. If any is retired, the corresponding feature breaks hard. `groq>=0.12.0` is unpinned (major-bump risk).
- **FastF1 freshness.** `fastf1==3.4.0` is pinned; FastF1 tracks a live timing API that evolves season to season. Pinned old versions can fail to load current-season sessions. Acceptable for now but must be revisited at season start.
- **Hardcoded circuit map.** `_fetch_circuit_id_by_name` (`history.py:101-165`) is a static dict; missing/renamed circuits silently fail `/history`.

### 2.5 Performance issues
- **Redundant Tavily "advanced" searches** (2.2 #3) — the most expensive and slowest dependency, fired too often.
- **Uncached Jolpi calls** (2.1) — repeated identical fetches within minutes.
- **No connection pooling** (2.1).
- **FastF1 `session.load()` is heavy** (download + parse). Correctly off-loaded via `to_thread` in `strategy`/`lap`/`rewind`, but there's no global concurrency cap; several simultaneous heavy loads can spike memory, and FastF1's disk cache isn't safe for concurrent writes to the same session.

### 2.6 Repo hygiene / security
- **Compiled bytecode is committed.** `__pycache__/*.pyc` are tracked (`git ls-files` shows `main.cpython-313.pyc`, all handler `.pyc`s). `.gitignore` only contains `.env`. Stale tracked `.pyc` can cause "old code runs after deploy" confusion.
- **Stale secret name.** `.env` still defines `ELEVENLABS_API_KEY` though ElevenLabs was removed (commit `824aabf`). Clean up to avoid confusion.
- **`.env` correctly untracked** — good. Keep it that way.

---

## 3. Proposed roadmap

| # | Priority | Feature / Fix | Why it matters | Complexity | Dependencies | Session size | Recommended timing |
|---|----------|---------------|----------------|------------|--------------|--------------|--------------------|
| 1 | P0 | Fix `/predict` & `/fantasy` async-coroutine bug | Two advertised commands are 100% broken | Trivial | none | XS | **Now** |
| 2 | P0 | Repo hygiene: gitignore + untrack `.pyc`, drop dead code & stale env | Prevents stale-deploy bugs; clarifies intent | Trivial | none | XS | **Now** |
| 3 | P1 | Persist `/notify` subscribers + seen-news (JSON/SQLite) | Stops silent unsubscribe on every restart | Low | none | S | **Now** |
| 4 | P1 | Caching + shared HTTP client for Jolpi calls | Avoids 429s; big latency win; no UX change | Low–Med | none | M | **Now** |
| 5 | P1 | Tighten `/ask` routing (search + standings) | Cuts cost/latency; improves accuracy on history Qs | Low | none | S | **Now** |
| 6 | P2 | Unify outbound sending through safe sender | Removes markdown parse failures everywhere | Low | none | S | Soon |
| 7 | P2 | Lightweight observability (timing, error counts, structured logs) | Makes everything after this measurable/debuggable | Low | none | S | Soon |
| 8 | P2 | Interactive Race-Weekend Hub (`/race` + inline buttons) | Foundation for all interactivity; flagship UX | Medium | #6 | M | Soon |
| 9 | P3 | `/driver` & `/team` profile cards | High-value, reuses existing data | Medium | #4 | M | Later |
| 10 | P3 | `/circuit` intelligence page | Race-weekend value | Medium | #4, search | M | Later |
| 11 | P3 | Auto post-session reaction summary (quali/race) | "Useful on race weekends" payoff; reuses `/result`,`/strategy` | Medium | #3, scheduler | M | Later |
| 12 | P3 | Personalized alerts (`/follow` a driver/team) | Stickiness; filters news/reminders | Medium | #3 | M | Later |
| 13 | P4 | Tone modes (`/tone`) + voice tweaks | Fun/personality; small | Low | none | S | Later |
| 14 | P4 | Strategy/stint charts (image output) | Visual upgrade for `/strategy`,`/lap` | Medium | matplotlib | M | Later |
| 15 | P4 | Tests + CI smoke | Locks in reliability gains | Medium | #1–#5 | M | Ongoing |
| — | Defer | Live timing telemetry stream | Fragile, high effort, low incremental value now | High | FastF1 livetiming | L | Deferred |
| — | Defer | Full UI localization (multi-language) | LLM tone mode covers most of the value | High | none | L | Deferred |
| — | Reject | Betting/odds integration | Policy/compliance risk | — | — | — | Rejected |
| — | Reject | DB/ORM migration, webhooks rewrite | Polling + JSON/SQLite is sufficient at this scale | — | — | — | Rejected |

Complexity: Trivial < Low < Medium < High. Session size: XS (≈15 min) < S < M < L.

---

## 4. Session plan

Each session is scoped to one focused, safe coding pass with explicit rollback. Sessions are ordered so value lands early and risk stays low.

### Session 1 — Fix the broken `/predict` and `/fantasy` (P0)
- **Goal:** Both commands return real previews again.
- **Files:** `handlers/predict.py`, `handlers/fantasy.py`.
- **Tasks (in order):**
  1. In each, change the gather so the async coroutine is awaited directly and only the sync FastF1 call is threaded:
     `next_race, qual_data = await asyncio.gather(get_next_race_info(), asyncio.to_thread(get_qualifying_results))`.
  2. Confirm no other `to_thread(<async fn>)` exists (`grep -rn "to_thread(get_next_race_info"`).
- **Tests:** Run `/predict` and `/fantasy` against the live bot (or a tiny asyncio repro calling the handler's data block). Assert: no `TypeError`, no "coroutine was never awaited" warning, `next_race` is a dict.
- **Rollback:** Revert the two-line change per file.
- **Definition of done:** Both commands produce a coherent text reply; logs are warning-free.

### Session 2 — Repo hygiene & dead-code cleanup (P0)
- **Goal:** Clean tree, no tracked bytecode, no misleading dead code.
- **Files:** `.gitignore`, `utils/f1_data.py`, `handlers/notify.py`, `main.py`, `.env` (local only).
- **Tasks:**
  1. Add `__pycache__/`, `*.pyc`, `fastf1_cache/`, `*.json` data files (if added later) to `.gitignore`; `git rm -r --cached **/__pycache__`.
  2. Delete dead `get_next_event` (`f1_data.py`) and unused `NEWS_SOURCES` (`notify.py`).
  3. Remove the stale `ELEVENLABS_API_KEY` line from `.env`.
  4. Decide on `testvoice`: gate behind an `ADMIN_CHAT_ID` env check or remove from `main.py:129`.
- **Tests:** `python -c "import main"` imports cleanly; `git status` clean after commit; bot boots and `/start` lists commands.
- **Rollback:** `git revert` the hygiene commit.
- **Definition of done:** No `.pyc` tracked, dead code gone, bot imports and starts.

### Session 3 — Persist `/notify` subscribers + seen-news (P1)
- **Goal:** Subscriptions and dedup survive restarts.
- **Files:** new `utils/store.py` (tiny JSON-or-SQLite KV), `handlers/notify.py`.
- **Tasks:**
  1. Add `utils/store.py`: `load(key, default)` / `save(key, value)` backed by a single JSON file (path from `DATA_DIR` env, default `./data`), or stdlib `sqlite3` if preferred. Atomic write (temp file + rename).
  2. On `setup_scheduler`, load `_subscribers` and `_seen_news_hashes` from store.
  3. On subscribe/unsubscribe and on `_mark_seen`, persist. Keep the in-memory structures as the hot path; store is write-through.
- **Tests:** Subscribe via `/notify`; restart the process; confirm `_subscribers` reloaded and `/notify` reports "already on". Add a fake seen-hash, restart, confirm present.
- **Rollback:** Revert; in-memory behaviour returns (no data loss beyond the new file).
- **Definition of done:** A restart preserves subscribers and news dedup.

### Session 4 — Jolpi caching + shared HTTP client (P1)
- **Goal:** Repeated data reads are cached; one pooled session; fewer 429s.
- **Files:** new `utils/http.py` (shared `aiohttp.ClientSession` singleton + `get_json(url, ttl)` with a small TTL dict cache), `utils/f1_data.py`, `handlers/history.py`, `main.py` (close session on `post_shutdown`).
- **Tasks:**
  1. `utils/http.py`: lazy global session; `get_json(url, ttl_seconds)` returning cached JSON within TTL (bounded dict, LRU-ish eviction).
  2. Route the Jolpi GETs in `f1_data.py` (standings, results, round resolution) and `history.py` (career/circuit paging) through `get_json`. TTLs: standings/results 600s, schedule/round-resolution 21600s, career 3600s.
  3. Register `post_shutdown` in `main.py` to close the session.
- **Tests:** Call `/standings` twice within TTL; assert the second is served from cache (log line / timing). Force expiry; assert refetch. Existing handlers behave identically.
- **Rollback:** Revert; helpers fall back to per-call sessions.
- **Definition of done:** Duplicate commands within TTL don't re-hit Jolpi; no behaviour change for users; session closes cleanly on shutdown.

### Session 5 — Smarter `/ask` routing (P1)
- **Goal:** Only fetch live data / search when actually needed.
- **Files:** `handlers/ask.py`.
- **Tasks:**
  1. Remove ultra-generic terms (`driver`, `team`, `best`, `worst`, `pick`, `season`) from `LIVE_KEYWORDS`, or gate them behind an additional "currentness" signal (a year ≥ current, or words like `now/latest/current/this season`).
  2. Separate "needs current standings" from "needs web search" so a 2008-title question doesn't pull 2026 standings.
  3. Keep the existing data-grounding for genuinely current queries.
- **Tests:** Table-driven unit test with `search`/data functions monkeypatched: assert which sources fire for ~10 representative queries (history vs current vs standings vs quali).
- **Rollback:** Revert keyword lists.
- **Definition of done:** Historical/technical questions skip Tavily; current questions still ground correctly.

### Session 6 — Unify outbound sending (P2)
- **Goal:** Every message goes through one safe sender; no parse failures anywhere.
- **Files:** `utils/telegram_safe.py`, `main.py`, `handlers/notify.py`.
- **Tasks:**
  1. Add a `safe_send(bot, chat_id, text, ...)` sibling to `safe_reply` for scheduler/news pushes.
  2. Route `start_handler`, `_send_reminder`, breaking-news push through the safe sender.
  3. (Optional) Add a MarkdownV2 escaper helper if standardizing on V2; otherwise keep legacy Markdown + plain-text fallback.
- **Tests:** Send strings containing `_ * [ ] ( )`; assert no `BadRequest` and a message is delivered.
- **Rollback:** Revert; direct `send_message` calls return.
- **Definition of done:** No handler can crash on markdown; reminders/news use the safe path.

### Session 7 — Lightweight observability (P2)
- **Goal:** Per-command timing + error counts + structured context, with near-zero overhead.
- **Files:** new `utils/metrics.py` (or a small decorator), apply across handlers via a shared wrapper; `main.py` error handler logs command + user.
- **Tasks:**
  1. Add a handler decorator/middleware that logs command name, latency, and outcome (ok/error) at INFO/ERROR.
  2. Count errors per command in-process; expose via an admin-only `/stats` (gated by `ADMIN_CHAT_ID`).
- **Tests:** Trigger a command; confirm a timing log line; trigger a failure; confirm error count increments.
- **Rollback:** Remove decorator usage.
- **Definition of done:** Every command emits one structured timing line; admin can read basic stats.

### Session 8 — Interactive Race-Weekend Hub (P2, flagship UX)
- **Goal:** `/race` becomes a hub with inline buttons that route to existing actions.
- **Files:** `handlers/race.py`, new `handlers/menu.py` (CallbackQuery router), `main.py` (register `CallbackQueryHandler`).
- **Tasks:**
  1. Append `InlineKeyboardMarkup` to `/race`: buttons **Predict**, **Fantasy**, **Standings**, **Add reminder**.
  2. `menu.py`: a `CallbackQueryHandler` that dispatches `callback_data` to the corresponding existing handler logic (extract shared functions so both command and button reuse them).
  3. Use `answer_callback_query` + edit-in-place or follow-up message.
- **Tests:** `/race` shows buttons; tapping each runs the right action; no "query is too old" errors (answer promptly).
- **Rollback:** Remove the keyboard + `CallbackQueryHandler` registration; commands still work standalone.
- **Definition of done:** A user can run a full race-weekend flow from buttons without typing commands.

### Sessions 9–15 (later phase, one each)
- **9 — `/driver` & `/team` profiles:** new `handlers/profile.py`; reuse standings + Ergast driver/constructor endpoints (cached). Tests: known driver returns title/wins/team; unknown returns friendly miss.
- **10 — `/circuit [name]`:** new `handlers/circuit.py`; layout, lap record, recent winners (Ergast) + a short search-grounded note. Tests: 3 known circuits resolve; unknown falls back gracefully.
- **11 — Auto post-session reactions:** extend `notify.py` scheduler to fire a summary ~20 min after each session end, reusing `/result` + `/strategy` builders; push to subscribers. Tests: simulate a past session end → one summary, deduped, not double-sent on restart.
- **12 — `/follow`:** store per-user favourites (via Session-3 store); highlight/filter reminders + news. Tests: follow VER → reminders tagged; unfollow clears.
- **13 — `/tone`:** per-chat tone setting injected into `SYSTEM_PROMPT`; persisted. Tests: tone persists; affects reply style.
- **14 — Strategy charts:** matplotlib stint/gap chart for `/strategy` and `/lap`, sent as photo. Tests: chart renders, sent within Telegram size limits; text fallback if matplotlib unavailable.
- **15 — Test suite + CI smoke:** pytest for data parsers (Ergast/FastF1 fixtures), routing logic, store; GitHub Actions import-and-unit smoke. Tests: green CI.

---

## 5. Recommended implementation order (safest path to value)

1. **Session 1 (fix broken commands)** — highest impact, lowest risk, restores two features immediately.
2. **Session 2 (hygiene)** — clears the deck so later deploys are trustworthy.
3. **Session 3 (persist notify)** — stops ongoing silent data loss.
4. **Session 4 (cache + pooled HTTP)** — removes the main reliability/latency risk before adding features that lean on data.
5. **Session 5 (ask routing)** — cheap accuracy/cost win.
6. **Session 6 + 7 (safe send + observability)** — make the system robust and measurable before building UX on top.
7. **Session 8 (hub)** — the flagship interactivity foundation; everything visual builds on it.
8. **Sessions 9–14** — feature expansion, one isolated module per session.
9. **Session 15** — lock the gains in with tests, ongoing.

Rationale: fixes and reliability first (no new surface area), then the one structural UX enabler (callbacks/menus), then additive feature modules that each fail independently and never block the core.

---

## 6. Deferred ideas (and why)
- **Live timing / telemetry streaming.** FastF1's livetiming is finicky, session-dependent, and high-maintenance; the payoff (true real-time) is narrow vs. effort. Revisit only if there's clear demand. A cheaper "session is live now" status flag (from the schedule) captures most of the value.
- **Full multi-language UI.** Real localization is a large, ongoing cost. The LLM tone/language mode (Session 13) delivers ~80% of the value at ~5% of the cost; defer full i18n.
- **Image-heavy dashboards / pinned race-day boards.** Valuable but depends on the hub (Session 8) and charts (Session 14) landing first; defer until those are stable.
- **Migrating off Ergast/Jolpi entirely.** Worth monitoring (it's the top strategic data risk), but aggressive caching (Session 4) buys runway. Plan a follow-up to evaluate FastF1-native standings/results once the mirror's reliability is understood.

## Rejected ideas
- **Betting/odds integration** — policy and compliance exposure; out of scope.
- **Database/ORM migration and webhook rewrite** — polling + a JSON/SQLite store is sufficient at this scale; a rewrite adds risk without proportional benefit.

---

## 7. Final recommendation — the next 3 actions to approve first

1. **Approve Session 1** — fix the `asyncio.to_thread(get_next_race_info)` bug in `predict.py` and `fantasy.py`. ~15 minutes, restores two broken commands, near-zero risk.
2. **Approve Session 3** — add a tiny JSON/SQLite store so `/notify` subscribers and news dedup survive restarts. The bot is currently losing every subscriber on each deploy.
3. **Approve Session 4** — put caching + a shared HTTP client in front of Jolpi/Ergast. This removes the biggest reliability risk (rate-limited mirror of a deprecated API) and is a prerequisite for every data-driven feature that follows.

Do these three before any new features. They're small, independently shippable, and each makes the bot meaningfully more reliable. Sessions 2 and 5 are equally cheap and can be bundled into the same week.
