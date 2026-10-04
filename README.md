# BoxBox

A Telegram bot that turns Formula 1 data into conversational answers. Ask it anything about F1 in chat, get a session countdown, predict the next winner from qualifying form, break down tyre strategies after a race, or send a voice note and have it transcribed and answered.

BoxBox combines live timing data from [FastF1](https://github.com/theOehrly/Fast-F1), web search via [Tavily](https://tavily.com/), and LLM, speech-to-text and text-to-speech via [OpenRouter](https://openrouter.ai/) behind a [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) interface.

---

## Features

| Command | What it does |
| --- | --- |
| `/start`, `/help` | Welcome message and command list |
| `/race` | Next race weekend countdown with every session time in Irish time |
| `/predict` | Pre-race winner prediction based on qualifying results, recent form, and circuit history |
| `/strategy` | Post-race tyre strategy breakdown — optimal vs actual stints per driver |
| `/fantasy` | F1 Fantasy picks for the upcoming round |
| `/standings` | Current drivers' and constructors' championship standings |
| `/rumour [topic]` | Latest paddock rumours, flagged as confirmed vs speculation |
| `/ask [question]` | Any F1 question, with live web search grounding |
| `/lap [driver] [session]` | Fastest lap summary (e.g. `/lap VER Q`) with sector deltas vs the session best |
| `/h2h [driver1] [driver2]` | Head-to-head stats for the current season (qualifying, races, points) |
| `/history [driver] [circuit]` | A driver's past results at a given track |
| `/career [driver]` | Complete career statistics for a driver |
| `/driver [name]` | Driver profile card — season standing plus career stats and a scouting note |
| `/team [name]` | Constructor profile card — season standing and current race line-up |
| `/rewind [circuit] [year]` | Relive the key moments and turning points of any past race |
| `/result` | Latest race result — finishing order with gap times and concise DNF reasons |
| `/follow [driver/team]` | Follow a driver or team so their breaking news is flagged for you (`/unfollow` to stop) |
| `/grid` | Starting-grid graphic for this weekend: official qualifying order with reported grid penalties applied (provisional until the race), real team colours from F1 timing. Also sent automatically when you ask about the grid |
| `/teammates` | Norris vs Piastri: points, qualifying and race head-to-heads, recent form, with a verdict |
| `/title` | Championship maths: who is still mathematically alive and what McLaren needs from here |
| `/pace` | McLaren race pace vs Mercedes, Ferrari and Red Bull over the last 3 races (upgrade watch, from FastF1 lap data) |
| `/debrief` | Spoken McLaren debrief of the last race (voice note plus text) |
| `/stewards [question]` | Penalties and stewards' decisions read straight from the official FIA documents (PDFs) |
| `/reset` | Clear the conversation memory |
| `/me` | What the bot remembers about you long-term (favourite driver, F1 Fantasy team); `/me clear` forgets it |
| `/notify` | Toggle session reminders, McLaren qualifying/race result alerts and breaking-news alerts |
| Plain text | In a private chat just type: the model fetches standings, results, McLaren analysis, news and FIA documents itself (tool calling), follow-ups like "and Piastri?" work, and replies stream in as they are written |
| Voice note | Transcribed and answered like a typed question (all the McLaren data above works by voice too) |
| Photo / screenshot | Send an image (timing screen, graphic, post, F1 Fantasy team) with an optional caption and ask about it. Fantasy teams are remembered for `/fantasy` |
| PDF | Send a PDF (FIA decision, technical directive) and ask about it |

McLaren result alerts check for new qualifying/race results every 10 minutes and message `/notify` subscribers. The system prompt also gets an auto-refreshed standings block, so answers stay current without editing the prompt. Session reminders fire 30 minutes before each session begins. The breaking-news watcher polls a curated list of F1 outlets (formula1.com, autosport.com, motorsport.com, the-race.com, gpfans.com, planetf1.com, f1i.com) every 30 minutes and pushes anything matching the breaking-keyword list to subscribers.

---

## Architecture

```
main.py                   Entry point: wires up handlers, scheduler, and the Telegram polling loop
handlers/
  ask.py                  /ask — LLM answer grounded in Tavily search
  race.py                 /race — next-weekend countdown from FastF1 schedule
  predict.py              /predict — qualifying + form → LLM prediction
  strategy.py             /strategy — FastF1 lap data → stint analysis
  fantasy.py              /fantasy — picks based on form, value, and circuit fit
  rumour.py               /rumour — searches paddock rumour sources
  standings.py            /standings — driver + constructor tables
  lap.py                  /lap — fastest lap + sector breakdown
  h2h.py                  /h2h — head-to-head season comparison
  history.py              /history, /career — historical driver data
  profile.py              /driver, /team — profile cards over standings + Ergast data
  follow.py               /follow, /unfollow — per-chat favourites, persisted; flags news
  rewind.py               /rewind — narrative replay of a past race
  notify.py               /notify subscriptions, session reminders, breaking-news poller
  result.py               /result — latest race finishing order + DNF reasons via Tavily
  menu.py                 CallbackQuery router for the /race inline-button hub
  voice.py                Voice note → transcription → answer → spoken reply (send_voice_reply)
  photo.py                Photo/screenshot and PDF questions (vision model, with the F1 tools)
  stewards.py             /stewards — penalties from the official FIA documents
  grid_cmd.py             /grid and the auto-attached grid graphic
  mclaren_cmds.py         /teammates, /title, /pace, /debrief
utils/
  f1_data.py              FastF1 wrappers, schedule helpers, Irish-time formatting
  groq_client.py          OpenRouter chat (tool loop, streaming, prompt caching, structured output, failover), speech-to-text and TTS (gpt-audio → edge-tts → gTTS fallback), token trimming
  mclaren.py              McLaren data layer (Jolpi): live snapshot, team-mate H2H, title maths, debrief, alert markers
  grid.py                 Grid penalties (FIA documents + news → structured output) and provisional starting grid
  f1_tools.py             Tools the model calls while answering (data, McLaren analysis, search, FIA docs, user memory)
  fia.py                  FIA documents page scraper + PDF reader
  userprefs.py            Long-term per-user facts (/me)
  graphics.py             Pillow-drawn reply graphics (grid); data-driven, never AI-generated
  sessions.py             Auto-collects every session from F1 live timing after it ends
  news.py                 Rolling 48h news digest injected into answers
  pace.py                 Race-pace comparison from FastF1 laps, cached
  convo.py                Per-chat conversation memory (last 20 exchanges, 24h expiry, persisted)
  tavily_client.py        Tavily search wrapper + result formatter
  rate_limit.py           Per-user rate limiter
  telegram_safe.py        Safe reply helper (splits long messages for Telegram's 4096-char limit)
```

The bot runs as a single long-lived polling process. APScheduler handles the reminder and news-watcher jobs in the same event loop.

### Models

Defined in [utils/groq_client.py](utils/groq_client.py):

All models are served through OpenRouter and can be overridden with env vars:

- `FAST_MODEL` (`openai/gpt-6-luna`) — fast path (race summaries, news summarisation, short lookups)
- `SMART_MODEL` (`anthropic/claude-sonnet-5.5`) — main answer model (`/ask`, `/predict`, `/strategy`, `/rumour`, ...)
- `STT_MODEL` (`google/gemini-3.5-flash-lite`) — voice-note transcription
- `FALLBACK_MODELS` (comma-separated, optional) — failover models tried if the primary errors or is retired; the other tier's model is always the last resort
- `TTS_MODEL` (`openai/gpt-audio-mini`) and `TTS_VOICE` (`cedar`) — spoken replies, falling back to edge-tts then gTTS

---

## Getting started

### Prerequisites

- Python 3.10+
- A [Telegram bot token](https://core.telegram.org/bots#how-do-i-create-a-bot) from `@BotFather`
- An [OpenRouter API key](https://openrouter.ai/keys)
- A [Tavily API key](https://app.tavily.com/) (free tier is enough for personal use)

### Install

```bash
git clone https://github.com/<you>/f1-bot.git
cd f1-bot
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Configure

Create a `.env` file in the project root:

```dotenv
TELEGRAM_TOKEN=your_telegram_bot_token
OPEN_ROUTER_KEY=your_openrouter_api_key
TAVILY_API_KEY=your_tavily_api_key
TELEGRAM_CHAT_ID=your_personal_chat_id   # optional, only used for admin pings

# Voice replies — edge-tts is used by default (no API key required).
EDGE_TTS_VOICE=en-GB-RyanNeural   # optional; run `edge-tts --list-voices` for options
TTS_SPEED=1.18                    # optional; atempo speed multiplier for the gTTS fallback (0.5–2.0)
```

`.env` is already in [.gitignore](.gitignore) — never commit it.

### Run

```bash
python main.py
```

The bot will register its command list with Telegram on first start and begin polling. Message your bot on Telegram to test.

### Deploy

A [Procfile](Procfile) is included for platforms like Railway, Render, or Heroku-style buildpacks:

```
worker: python main.py
```

Set the same environment variables in your platform's dashboard. The bot is a single worker process — no web port, no database, no Redis required. FastF1 caches data to the platform's temp directory.

---

## Customisation

- **Timezone** — session times are formatted in `Europe/Dublin` by default. Change `IRISH_TZ` in [utils/f1_data.py](utils/f1_data.py) to your timezone.
- **Tone and voice** — edit `SYSTEM_PROMPT` in [utils/groq_client.py](utils/groq_client.py). The current prompt biases towards a "race engineer talking to a smart fan" tone and bans common LLM filler phrases.
- **News sources** — `NEWS_SOURCES` and `BREAKING_KEYWORDS` in [handlers/notify.py](handlers/notify.py) control what the watcher considers breaking news.
- **Models** — swap the `FAST_MODEL` / `SMART_MODEL` constants in [utils/groq_client.py](utils/groq_client.py) for any other OpenRouter model (or set the env vars above).
- **Rate limit** — adjust the window in [utils/rate_limit.py](utils/rate_limit.py).

---

## Data and accuracy notes

- Live timing data is whatever FastF1 has loaded. Sessions usually appear in the FastF1 dataset within an hour or two of running. Pre-session, `/predict` and `/strategy` will return a friendly error.
- LLM responses are grounded in Tavily search results where applicable, but they're still LLM output. The system prompt tells the model to flag uncertainty rather than confabulate, but treat anything time-sensitive as "best effort, verify before betting your fantasy team on it."
- Historical data via FastF1 generally covers 2018 onwards in detail; earlier seasons have results but limited telemetry.

---

## Dependencies

See [requirements.txt](requirements.txt). The notable ones:

- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) `21.6` — Telegram client
- [fastf1](https://github.com/theOehrly/Fast-F1) `3.4.0` — F1 timing data
- [httpx](https://www.python-httpx.org/) — OpenRouter API calls
- [tavily-python](https://github.com/tavily-ai/tavily-python) `0.3.9` — web search
- [apscheduler](https://github.com/agronholm/apscheduler) `3.10.4` — reminder + news jobs

---

## License

MIT. See [LICENSE](LICENSE) if present, or add one before publishing.

---

## Acknowledgements

- F1 timing data courtesy of [FastF1](https://github.com/theOehrly/Fast-F1), which wraps the public Ergast and live timing APIs.
- This project is unaffiliated with Formula 1, the FIA, or any team. F1 trademarks belong to their respective owners.
