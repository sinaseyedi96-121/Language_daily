# German Daily Bot

Sends a daily German (B1) reactivation lesson to a Telegram channel, generated
by Claude Haiku — built for relearning German after a long gap, not learning
from zero.

Each day: 3 new B1 words + 2 words due for spaced-repetition review (1 → 3 →
7 → 16 → 35 → 90 day intervals), a rotating grammar tip from a 20-topic B1
curriculum, and 3 review questions.

## Run locally
```bash
pip3 install -r requirements.txt
cp .env.example .env      # then fill in your real values
python3 german_daily_bot.py --once
```

## Run automatically (GitHub Actions)
Daily run is handled by `.github/workflows/italian-daily.yml` (filename kept
from the original Italian version — only the content changed).
Credentials come from repository Secrets:
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `ANTHROPIC_API_KEY`.

`progress.json` stores each word's review schedule and the current grammar
topic. The workflow commits it back to the repo after every successful run
— don't delete it once it exists, or review scheduling resets.
