# IronMate 💊

IronMate is a warm, thoughtful companion and wellness reminder Telegram bot built to look after a friend's daily supplement routine (iron and multivitamins).

It pairs proactive, context-aware reminders with natural, casual Roman-script Hinglish conversations powered by local LLM inference (via Ollama) and a fast deterministic precedence router.

---

## Key Features

- **Personalized Supplement Tracking:**
  - Automated intake logging in SQLite (`supplements.db`).
  - Consecutive streak calculation for consistency and motivation.
  - Distinction between morning multivitamins and evening iron pills.

- **Intelligent Precedence Router:**
  - **Snooze / Busy Detection:** Understands when your friend is busy in class, traffic, driving, or meetings, and dynamically postpones reminders.
  - **Beverage Protection:** Automatically suggests waiting 45 minutes if your friend just had chai, coffee, or milk (tannins inhibit iron absorption).
  - **Intake Negation Detection:** Detects phrases like *"iron lena bhool gayi"* or *"didn't take my iron yet"* without false-logging.
  - **Context-Aware Chit-Chat:** Routes affectionate check-ins, banter, questions, and health follow-ups.

- **Warm & Empathetic Persona:**
  - Texts like a caring friend in authentic, casual Hinglish.
  - Fast response dispatch through an extensible reply bank with fallback to local Ollama models (`gemma3:4b` or `llama3.2:3b`).
  - Strict multi-layer output validator ensuring grounded, caring, and respectful messages without repetitive assistant phrases or inappropriate language.

- **Interactive Telegram UI:**
  - Inline buttons for low-friction logging: `✅ Took Iron`, `💊 Took Vitamin`, `⏰ Snooze 30m`, `☕ Just had Chai`.
  - Proactive cron scheduling for morning (10:00 AM) and evening (8:30 PM) routines.
  - Auto-binding to chat IDs on `/start`.
  - Built-in tuning feedback mode with 👍 / 👎 ratings and `/last` inspection for the bot owner.

---

## Architecture & Tech Stack

- **Runtime:** Python 3.11+
- **Bot Framework:** `python-telegram-bot` (AsyncIO)
- **Scheduler:** `APScheduler` (AsyncIOScheduler for cron & dynamic reminders)
- **Storage:** SQLite (`db.py` for persistent intake logs and streak tracking)
- **Inference Engine:** `ollama` (Local LLM execution with strict temperature gating)

---

## Directory Structure

```text
├── .gitignore             # Git ignore configuration
├── Readme.md              # Project documentation
├── agent.py               # Conversational agent, router, validator, and bank integration
├── bot.py                 # Telegram bot handlers, lifecycle hooks, and APScheduler integration
├── db.py                  # Database operations and streak management
└── requirements.txt       # Project dependencies
```

---

## Getting Started

### 1. Prerequisites
- Python 3.11 or higher
- [Ollama](https://ollama.ai/) installed and running locally with your desired model:
  ```bash
  ollama pull gemma3:4b
  ```

### 2. Installation
Clone the repository and install dependencies:
```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux/macOS
source .venv/bin/activate

pip install -r requirements.txt
```

### 3. Environment Configuration
Create a `.env` file in the project root:
```env
TELEGRAM_BOT_TOKEN="your_telegram_bot_token"
TARGET_CHAT_ID="numeric_chat_id_or_leave_empty_for_auto_bind"
IRONMATE_MODEL="gemma3:4b"

# Optional: Tuning mode
IRONMATE_TUNING="0"
IRONMATE_OWNER_ID="your_telegram_user_id"
```

### 4. Running the Bot
Launch the Telegram polling bot:
```bash
python bot.py
```
Open Telegram, message your bot, and send `/start` to begin!

---

## Verification

To verify your environment setup and check bot initialization:
```bash
python -c "import bot, agent, db; print('IronMate modules loaded successfully!')"
```

---

## Privacy & Safety

IronMate is strictly designed for local, private operation. All database records, chat logs, filter lists, and style pools are stored locally and are excluded from version control.