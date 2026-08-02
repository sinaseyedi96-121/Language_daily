# German Daily Bot

Sends a daily German (B1) reactivation lesson to a Telegram channel, generated
by Claude Haiku — built for relearning German after a long gap, not learning
from zero. Every word, usage note, word-family entry, example sentence, and
grammar tip is translated into both English and Turkish.

Each day: 3 new B1 words + 2 words due for spaced-repetition review (1 → 3 →
7 → 16 → 35 → 90 day intervals), a rotating grammar tip from a 20-topic B1
curriculum, and 3 review questions — plus one voice note with just the
German text (headwords + example sentences) read aloud, so you can listen
to pronunciation without the English/Turkish translations mixed in.

## Voice notes (Microsoft Edge TTS)
Voice is generated with [`edge-tts`](https://github.com/rany2/edge-tts) (free,
uses the same TTS service as Microsoft Edge's Read Aloud) using the German
neural voice `de-DE-KatjaNeural`, then converted to ogg/opus with `ffmpeg`
(required on PATH) so Telegram delivers it as a proper voice-note bubble.
Only the `german`/`de` fields from the lesson are spoken — English and
Turkish text is never sent to TTS. Toggle with `ENABLE_VOICE` and change the
voice with `TTS_VOICE` in `german_daily_bot.py`.

## Run locally
```bash
pip3 install -r requirements.txt
brew install ffmpeg       # or your OS's package manager
cp .env.example .env      # then fill in your real values
python3 german_daily_bot.py --once
```

## Run automatically (GitHub Actions)
Daily run is handled by `.github/workflows/german-daily.yml`.
Credentials come from repository Secrets:
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `ANTHROPIC_API_KEY`.

`progress.json` stores each word's review schedule and the current grammar
topic. The workflow commits it back to the repo after every successful run
— don't delete it once it exists, or review scheduling resets.
