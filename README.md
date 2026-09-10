# Orna Telegram Bot

A Telegram bot for [Orna RPG](https://playorna.com) guild-shop planning:
which guild sells which crafting material and when, how many guild proofs
you'll need to buy the amount you want, and reminders you can drop straight
into Google Calendar. It understands plain natural-language requests (English
or Ukrainian) as well as slash commands, and can read your screenshots via
OCR — both single-item stat screens and the guild "offerings" (altar
donation) screen.

## Features

- **`/res_today`** — which guilds have which materials for sale today.
- **`/res_next <material>`** — every guild that sells a material and the
  next date it appears.
- **Natural language** — just tell the bot what you need (e.g. *"Потрібно
  300 адамантину і 150 мітрилу"* or *"I need Adamantine and Mythril"*, or
  `/need Adamantine, Mythril`). It asks how many of each, then replies with
  every guild that sells it, the next date, and how many of that guild's
  proofs you'll need — with the date itself linking to a ready-to-save
  Google Calendar event (all-day, one per guild visit, bundling every
  material you need from that guild on that day).
- **Item screenshot assessment** — send a screenshot of an Orna item and the
  bot OCRs it, looks it up in the [playorna.com codex](https://playorna.com/codex/),
  and projects its stats across upgrade levels.
- **Guild offerings screenshot** — send a screenshot of the altar's "NEEDED
  OFFERINGS" screen (progress bars of `<have> / <need>` per material) and
  the bot computes the shortfall for each, resolves OCR'd/Ukrainian names
  back to English, and gives you the same guild-availability / proof-cost /
  calendar-link breakdown as the natural-language flow.
- Guild-proof cost math is ported from OrnaCodex's
  [`ProofView.vue`](https://github.com/67au/OrnaCodex/blob/main/src/views/ProofView.vue).

## How it fits together

```
telegram_bot.py            entry point — registers all handlers, runs polling
├─ telegram_assess.py      screenshot → OCR → item stats/quality assessment
│  └─ telegram_offerings.py   (screenshot is an offerings screen instead? hand off here)
├─ telegram_resources.py   natural-language "what do I need" conversation flow
│  └─ telegram_nlp.py      Ollama (gpt-oss:20b) calls: extract resource names / quantities
├─ orna_sheets.py          Google Sheets access (which guild sells what, when)
├─ orna_codex.py           playorna.com codex scraping (item stats, tier/rarity)
├─ orna_assess.py          upgrade-projection math for item assessment
├─ orna_proofs.py          guild-proof exchange-rate math (ProofView.vue port)
├─ orna_calendar.py        Google Calendar "add event" link builder
└─ orna_material_names_uk.py   static EN<->UK material name table (see below)
```

`orna_material_names_uk.json` is scraped once from the codex's own materials
list (English and Ukrainian listings, matched by item slug) and is the
*deterministic* source of truth for translating OCR'd Ukrainian material
names — it's tried before falling back to the LLM, which is not reliable
enough on its own for this (the same input can return a hit on one call and
a miss on the next). Regenerate it with `orna_scrape_material_names.py` if
the game adds new materials.

## Prerequisites

- Python 3.11+
- [Tesseract OCR](https://github.com/tesseract-ocr/tesseract), with the
  Ukrainian language pack (for the screenshot features)
- [Ollama](https://ollama.com), running locally with the `gpt-oss:20b`
  model pulled (for the natural-language and OCR name-resolution features)
- A Telegram bot token
- A Google Sheets API key, and a spreadsheet listing which guild sells which
  material on which date (see [Google Sheets setup](#4-google-sheets-api-key--spreadsheet) below)

## Installation

### 1. Clone and install Python dependencies

```bash
git clone https://github.com/ikruchynskyi/orna-telegram-bot.git
cd orna-telegram-bot
python3 -m venv venv
source venv/bin/activate         # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Install Tesseract OCR

- **macOS**: `brew install tesseract tesseract-lang`
- **Ubuntu/Debian**: `sudo apt install tesseract-ocr tesseract-ocr-ukr`
- **Windows**: [UB-Mannheim's installer](https://github.com/UB-Mannheim/tesseract/wiki)

If the bot reports `tesseract not found` even though it's installed, either
add its install directory to `PATH`, or point
`pytesseract.pytesseract.tesseract_cmd` (in `telegram_assess.py`) at the
absolute path of the binary — this commonly comes up when running the bot as
a background service (launchd/systemd) whose environment doesn't inherit
your shell's `PATH`.

### 3. Install Ollama and pull the model

```bash
# Install Ollama: see https://ollama.com/download
ollama pull gpt-oss:20b
ollama serve   # if it isn't already running as a service
```

The bot talks to Ollama's local REST API directly (`http://127.0.0.1:11434`
by default — override with `OLLAMA_HOST` if yours runs elsewhere).

### 4. Register a Telegram bot

1. Open a chat with [@BotFather](https://t.me/BotFather) on Telegram.
2. Send `/newbot` and follow the prompts (choose a name and a username
   ending in `bot`).
3. BotFather replies with your bot token — this is `BOT_TOKEN`.

### 5. Google Sheets API key & spreadsheet

1. Go to the [Google Cloud Console](https://console.cloud.google.com/),
   create (or pick) a project.
2. **APIs & Services → Library** → enable the **Google Sheets API**.
3. **APIs & Services → Credentials → Create Credentials → API key**.
   This key is `SHEETS_API_KEY`. (An API key is enough for read-only access
   to a spreadsheet shared as "Anyone with the link can view" — no OAuth
   needed.)
4. Create a Google Sheet (or use an existing one) with a tab named
   **`Material Forecast`**, laid out as:
   - Row 6: header row
   - Rows 7–68: one material per row
   - Column L: material name
   - Column M: Anguish (an outdated mechanic — present in the layout but
     ignored by the bot)
   - Columns N–W: the next appearance date for that material at each of the
     10 active guilds, **in this exact order**: Agony, Despair, Melancholy,
     Torment, Coral, Deepshards, Remembrance, Sparring, Trials, Towers.
     Dates are plain English `"Month Day"` strings (e.g. `"September 15"`).
   - Share the sheet as "Anyone with the link can view".
5. If you're not using the maintainer's spreadsheet, set `SPREADSHEET_ID`
   (and `SHEET_RANGE` if you changed the tab/range) in `.env` — see below.

### 6. Configure environment variables

```bash
cp .env.dist .env
```

Fill in `BOT_TOKEN` and `SHEETS_API_KEY` at minimum. See `.env.dist` for the
full list of optional overrides (custom spreadsheet, Ollama host/model/key).

### 7. Run it

```bash
python telegram_bot.py
```

### 8. (Optional) Keep it running in the background

**macOS (launchd)** — create `~/Library/LaunchAgents/com.yourname.ornabot.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.yourname.ornabot</string>
    <key>ProgramArguments</key>
    <array>
        <string>/path/to/orna-telegram-bot/venv/bin/python3</string>
        <string>/path/to/orna-telegram-bot/telegram_bot.py</string>
    </array>
    <key>WorkingDirectory</key><string>/path/to/orna-telegram-bot</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>/path/to/orna-telegram-bot/bot.log</string>
    <key>StandardErrorPath</key><string>/path/to/orna-telegram-bot/bot.err.log</string>
</dict>
</plist>
```

Then: `launchctl load ~/Library/LaunchAgents/com.yourname.ornabot.plist`

**Linux (systemd)** — create `/etc/systemd/system/orna-bot.service`:

```ini
[Unit]
Description=Orna Telegram Bot
After=network.target

[Service]
WorkingDirectory=/path/to/orna-telegram-bot
ExecStart=/path/to/orna-telegram-bot/venv/bin/python3 telegram_bot.py
Restart=always

[Install]
WantedBy=multi-user.target
```

Then: `sudo systemctl enable --now orna-bot`

## Usage

**Slash commands:**
- `/res_today` — resources available today, by guild
- `/res_next Adamantine` — every guild + date for a material
- `/need Adamantine, Mythril` — shortcut into the natural-language quantity flow
- `/cancel` — abort an in-progress conversation

**Natural language:** just message the bot what you need — no command
required:

```
You:  Потрібно 300 адамантину і 150 мітрилу
Bot:  Скільки одиниць кожного ресурсу вам потрібно?
      • Adamantine
      • Mythril
You:  300, 150
Bot:  <b>Adamantine</b> — потрібно 300
        Despair    September 10  сьогодні  135 × Proof of Despair
        ...
      (each date links to a Google Calendar draft)
```

**Screenshots:** just send a photo.
- A single item's stat screen → quality/upgrade assessment.
- The altar's "NEEDED OFFERINGS" screen → shortfall + guild/proof breakdown
  for everything you're short on.

## Maintenance

If the game adds new materials, regenerate the Ukrainian name table:

```bash
python orna_scrape_material_names.py
```

This re-scrapes `https://playorna.com/codex/items/?c=material` in both
English and Ukrainian and rewrites `orna_material_names_uk.json`.
