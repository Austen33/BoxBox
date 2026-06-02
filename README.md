# BoxBox

A Telegram bot that turns Formula 1 data into conversational answers. Ask it anything about F1 in chat, get a session countdown, predict the next winner from qualifying form, break down tyre strategies after a race, or send a voice note and have it transcribed and answered.

BoxBox combines live timing data from [FastF1](https://github.com/theOehrly/Fast-F1), web search via [Tavily](https://tavily.com/), and LLM responses from [Groq](https://groq.com/) (Llama 3.1/3.3 + Whisper) behind a [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) interface.

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
| `/rewind [circuit] [year]` | Relive the key moments and turning points of any past race |
| `/notify` | Toggle session reminders and breaking-news alerts |
| Voice note | Send a voice message — it's transcribed with Whisper and answered like `/ask` |

Session reminders fire 30 minutes before each session begins. The breaking-news watcher polls a curated list of F1 outlets (formula1.com, autosport.com, motorsport.com, the-race.com, gpfans.com, planetf1.com, f1i.com) every 30 minutes and pushes anything matching the breaking-keyword list to subscribers.

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
  rewind.py               /rewind — narrative replay of a past race
  notify.py               /notify subscriptions, session reminders, breaking-news poller
  voice.py                Voice-message ingest → Whisper → /ask flow
utils/
  f1_data.py              FastF1 wrappers, schedule helpers, Irish-time formatting
  groq_client.py          Groq Chat + Whisper client, system prompt, token trimming
  tavily_client.py        Tavily search wrapper + result formatter
  rate_limit.py           Per-user rate limiter
```

The bot runs as a single long-lived polling process. APScheduler handles the reminder and news-watcher jobs in the same event loop.

### Models

Defined in [utils/groq_client.py](utils/groq_client.py):

- `llama-3.1-8b-instant` — fast path (news summarisation, simple classification)
- `llama-3.3-70b-versatile` — main answer model
- `whisper-large-v3-turbo` — voice-note transcription

---

## Getting started

### Prerequisites

- Python 3.10+
- A [Telegram bot token](https://core.telegram.org/bots#how-do-i-create-a-bot) from `@BotFather`
- A [Groq API key](https://console.groq.com/keys)
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
GROQ_API_KEY=your_groq_api_key
TAVILY_API_KEY=your_tavily_api_key
TELEGRAM_CHAT_ID=your_personal_chat_id   # optional, only used for admin pings

# Voice replies (optional). With ElevenLabs set, voice notes use a natural,
# fluent voice; without it the bot falls back to Groq Orpheus, then gTTS.
ELEVENLABS_API_KEY=your_elevenlabs_api_key
ELEVENLABS_VOICE_ID=onwK4e9ZLuTAKqWW03F9   # optional; default is "Daniel" (calm, clear)
ELEVENLABS_MODEL=eleven_multilingual_v2    # optional
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
- **Models** — swap the `FAST_MODEL` / `SMART_MODEL` constants in [utils/groq_client.py](utils/groq_client.py) for any other Groq-hosted model.
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
- [groq](https://github.com/groq/groq-python) `0.9.0` — LLM + Whisper
- [tavily-python](https://github.com/tavily-ai/tavily-python) `0.3.9` — web search
- [apscheduler](https://github.com/agronholm/apscheduler) `3.10.4` — reminder + news jobs

---

## License

MIT. See [LICENSE](LICENSE) if present, or add one before publishing.

---

## Acknowledgements

- F1 timing data courtesy of [FastF1](https://github.com/theOehrly/Fast-F1), which wraps the public Ergast and live timing APIs.
- This project is unaffiliated with Formula 1, the FIA, or any team. F1 trademarks belong to their respective owners.
