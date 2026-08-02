"""
german_daily_bot.py
--------------------
Sends a daily German (A2/B1) reactivation lesson to a Telegram channel, generated
by Claude Haiku. Built for RECALLING forgotten everyday German after a
long gap, rather than learning from zero.

Each day's message contains:
  - A few brand-new A2/B1 words (English + Turkish translation, usage note,
    word family of common derivations, 3 example sentences). Nouns include
    their article (der/die/das) and plural, since gender is one of the
    first things that gets forgotten.
  - A few REVIEW words resurfacing on a spaced-repetition schedule
    (roughly 1 -> 3 -> 7 -> 16 -> 35 -> 90 days after each successful review).
  - One short grammar tip, cycling through a fixed B1 grammar curriculum.
  - One voice note (Telegram sendVoice) with just the German text -- the
    headwords and example sentences -- read aloud via Microsoft Edge TTS.
    English and Turkish text is never spoken.

State (which words exist, when they're next due, which grammar topic is
next) lives in progress.json, which the GitHub Actions workflow commits
back to the repo after every successful run.

Credentials are read from environment variables.
  - Locally:  a .env file (loaded by python-dotenv) provides them.
  - On GitHub Actions: repository Secrets provide them.
The .env file must NEVER be committed to GitHub (see .gitignore).

Run modes:
    python3 german_daily_bot.py --once     # generate + send once (used by GitHub Actions)
    python3 german_daily_bot.py            # local loop, sends daily at SEND_TIME

Requirements:
    pip3 install requests python-dotenv schedule edge-tts
    ffmpeg must also be installed and on PATH (used to convert the TTS
    output into the ogg/opus format Telegram voice notes require).
"""

import re
import os
import json
import html
import time
import asyncio
import argparse
import tempfile
import subprocess
from datetime import date, timedelta

import requests
import edge_tts
from dotenv import load_dotenv

load_dotenv()

# --------------------------------------------------------------------------- #
#  CREDENTIALS  — read from environment (.env locally / Secrets on GitHub)
# --------------------------------------------------------------------------- #
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")

# --------------------------------------------------------------------------- #
#  CONFIG
# --------------------------------------------------------------------------- #
MODEL        = "claude-haiku-4-5-20251001"   # cheapest current tier
LEVEL        = "A2/B1"
EXAMPLE_LEVEL = "B1/B2"      # example sentences are pitched a notch above word level

# ---- DAILY WORD MIX --------------------------------------------------------
# Change the numbers here to adjust how many NEW words you get per level.
# The order below is also the order they appear in the message (easiest first).
NEW_WORDS_PER_LEVEL = {
    "A2": 3,   # everyday basics to recall
    "B1": 3,   # your certified level
    "B2": 1,   # one small stretch word per day
}
REVIEW_PER_DAY_TARGET = 2   # review words on top of the new ones (when due)
# ----------------------------------------------------------------------------

# ---- VOICE (Microsoft Edge TTS) --------------------------------------------
ENABLE_VOICE = True
TTS_VOICE    = "de-DE-KatjaNeural"   # German neural voice, free via edge-tts
# ----------------------------------------------------------------------------

# Spaced-repetition intervals, in days, after the 1st/2nd/3rd/... successful
# review of a word. Stays at the last value for further reviews.
REVIEW_INTERVALS = [1, 3, 7, 16, 35, 90]

MAX_TOKENS   = 9000   # Turkish fields roughly double the translated text per word
TEMPERATURE  = 0.7

SEND_TIME     = "08:00"        # 24h, local machine time (only used in loop mode)
PROGRESS_FILE = "progress.json"

TELEGRAM_MSG_LIMIT = 4096
SAFE_CHUNK         = 3800     # leave margin below the hard limit

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

# Fixed B1 grammar curriculum, cycled through one topic per day (~20 days
# per full loop, which doubles as a natural grammar review interval).
GRAMMAR_TOPICS = [
    "Perfekt vs. Präteritum: when to use each in spoken vs. written German",
    "The four cases overview: Nominativ, Akkusativ, Dativ, Genitiv",
    "Dative prepositions: aus, bei, mit, nach, seit, von, zu, gegenüber",
    "Accusative prepositions: durch, für, gegen, ohne, um",
    "Two-way prepositions (Wechselpräpositionen): an, auf, hinter, in, neben, über, unter, vor, zwischen",
    "Word order: the verb in position 2 in main clauses",
    "Word order: verb-final in subordinate clauses (weil, dass, wenn, ob)",
    "Separable verbs (trennbare Verben) and where the prefix goes",
    "Modal verbs: können, müssen, dürfen, sollen, wollen, mögen",
    "Reflexive verbs and reflexive pronouns",
    "Adjective endings after der/die/das",
    "Adjective endings after ein/eine/ein and with no article",
    "Comparative and superlative forms",
    "Relative clauses with der/die/das as relative pronouns",
    "Konjunktiv II basics: würde + infinitive, wäre, hätte, könnte",
    "Passive voice basics: werden + Partizip II",
    "Genitive vs. spoken German 'von' for possession",
    "Time expressions: am, um, im, seit, vor + time",
    "Question words and question word order",
    "Connectors: weil vs. denn, obwohl, trotzdem, deshalb",
]

# --------------------------------------------------------------------------- #
#  PROMPT
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = f"""You are my German teacher. I once reached {LEVEL} and then
had a long gap with little practice, so I have forgotten many everyday words
and grammar rules. Your job is to help me RECALL {LEVEL}-level German --
practical, everyday language. This is about recovering basics, NOT about
stretching toward advanced German.

Each day I ask for a specific number of new words PER LEVEL. Calibrate each
level like this:
- A2 words: high-frequency everyday basics I may have forgotten, like:
  der Termin, abholen, die Rechnung, der Schlüssel, sich beeilen,
  die Verspätung. Simple, concrete, daily-routine words.
- B1 words: still everyday and practical but a step up, like: die Anmeldung,
  der Vertrag, verschieben, sich erkundigen, der Umzug, die Bewerbung.
  Situations: renting a flat, doctor visits, forms, trains, ordinary work.
- B2 words (only when I ask for them): useful, SPOKEN-German B2 -- words a
  native uses in normal conversation, like: sich Gedanken machen, die
  Voraussetzung, sich gewöhnen an, ausgerechnet, zuverlässig. NOT formal
  business/policy register.
- WRONG at any level -- never give words like: der Sachverhalt, der
  Stellhebel, das Regelwerk, die Entgegnung, die Stellungnahme, sich
  erübrigen. If a word mainly appears in reports, meetings-speak, or
  newspaper commentary, do not use it.
- Prefer concrete over abstract. A noun naming a thing, place, action, or
  everyday situation beats an abstract nominalization every time.
- Example sentences should be pitched at {EXAMPLE_LEVEL} level, regardless of
  the target word's own level: natural, complete sentences a fluent
  intermediate speaker would actually say out loud, comfortable using a
  subordinate clause and everyday connectors (weil, wenn, obwohl, deshalb,
  nachdem, bevor, damit...). Not simplistic A2 fragments, but still
  spoken-register German, not written/formal constructions.

Do NOT include any Farsi.
Do NOT conjugate verbs in the vocabulary list itself (give the infinitive) —
conjugated forms are fine ONLY inside the grammar tip, if that topic needs one.
For nouns, ALWAYS include the definite article (der/die/das) and the plural
form — gender is exactly the kind of thing that gets forgotten first.

I am learning German through BOTH English and Turkish, so every English
field below has a matching Turkish field. Produce natural, fluent Türkiye
Turkish (not Azerbaijani, not a literal word-for-word crib) — the kind a
Turkish speaker would actually say, using proper Turkish characters
(ğ, ı, İ, ş, ç, ö, ü). Do NOT include any Farsi in the Turkish text either.

Return ONLY valid JSON, no markdown, no code fences, no commentary, in
exactly this shape:
{{
  "new_words": [
    {{
      "german": "the word WITHOUT its article (e.g. 'Besorgnis', never 'die Besorgnis'), infinitive form if a verb",
      "level": "A2, B1, or B2 -- the level slot this word fills",
      "article": "der, die, or das if this is a noun -- otherwise null",
      "plural": "the plural form if this is a noun -- otherwise null",
      "english": "English translation",
      "turkish": "Turkish translation",
      "usage": "short usage note in English, or empty string if not useful",
      "usage_tr": "the same usage note in Turkish, or empty string if 'usage' is empty",
      "synonyms": ["2-3 German synonyms for this word, including der/die/das for noun synonyms -- empty list if no good synonym exists"],
      "family": [
        {{"word": "related word derived from the same root", "type": "noun/verb/adjective/adverb", "english": "translation", "turkish": "Turkish translation"}}
      ],
      "examples": [
        {{"de": "...", "en": "...", "tr": "..."}},
        {{"de": "...", "en": "...", "tr": "..."}},
        {{"de": "...", "en": "...", "tr": "..."}}
      ]
    }}
  ],
  "review_words": [
    // SAME shape as each item in "new_words" above, but "german" MUST
    // exactly match one of the words I ask you to review -- never invent
    // or substitute a different word here.
  ],
  "grammar_tip": {{
    "topic": "the topic I gave you",
    "explanation": "2-4 sentences, clear and practical, in English",
    "explanation_tr": "the same explanation in Turkish",
    "examples": [
      {{"de": "...", "en": "...", "tr": "..."}},
      {{"de": "...", "en": "...", "tr": "..."}}
    ]
  }}
}}

Rules:
- Provide EXACTLY the number of new words per level that I request, and
  order "new_words" from easiest to hardest (A2 first, then B1, then B2).
- "family" lists 2-4 words derived from the same root (e.g. for Besorgnis:
  besorgen, besorgt, besorgniserregend). Include the article for nouns in
  the "word" field here (e.g. "die Sorge"). Only include genuinely common,
  {LEVEL}-useful derivations -- if none exist, use an empty list.
- "synonyms" lists 2-3 real, commonly-used German words or short expressions
  that could replace the headword in casual speech without changing the
  meaning much (e.g. for sich beeilen: sich sputen, keine Zeit verlieren).
  Include der/die/das for noun synonyms. Never invent a weak or awkward
  synonym just to fill the quota -- an empty list is fine if none exist.
- Never put der/die/das inside the "german" field itself -- the article
  belongs ONLY in the "article" field.
- Every example sentence is natural, {EXAMPLE_LEVEL}-appropriate German.
- Every "turkish", "usage_tr", "explanation_tr", and "tr" field must be a
  faithful, natural translation of its English counterpart -- same meaning,
  same register, never abbreviated or skipped."""


def build_user_message(level_counts, review_words, known_words, grammar_topic):
    breakdown = ", ".join(f"{n} at {lvl} level" for lvl, n in level_counts.items() if n > 0)
    total = sum(n for n in level_counts.values() if n > 0)
    parts = [f"Give me {total} NEW words/expressions I haven't seen before: {breakdown}."]

    if known_words:
        parts.append(
            "Do NOT use any of these -- they're already covered: "
            + ", ".join(known_words)
        )
    else:
        parts.append("Nothing has been covered yet, so any solid everyday A2/B1 item is fine.")

    if review_words:
        parts.append(
            "Also give me fresh usage notes and 3 new example sentences for "
            "exactly these REVIEW words (use these exact words, unchanged): "
            + ", ".join(review_words)
        )
    else:
        parts.append('No review words are due yet -- leave "review_words" as an empty list.')

    parts.append(f"Grammar tip topic for today: {grammar_topic}")

    return "\n\n".join(parts)


# --------------------------------------------------------------------------- #
#  PROGRESS STORE  (vocab SRS state + grammar curriculum position)
# --------------------------------------------------------------------------- #
def load_progress():
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                data.setdefault("vocab", {})
                data.setdefault("grammar_index", 0)
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {"vocab": {}, "grammar_index": 0}


def save_progress(progress):
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)


def compute_next_review_date(review_count):
    idx = min(review_count, len(REVIEW_INTERVALS) - 1)
    return (date.today() + timedelta(days=REVIEW_INTERVALS[idx])).isoformat()


def get_due_review_words(vocab, limit):
    today_iso = date.today().isoformat()
    due = [w for w, info in vocab.items() if info.get("next_review", "9999-12-31") <= today_iso]
    due.sort(key=lambda w: vocab[w].get("next_review", ""))
    return due[:limit]


def update_vocab_after_send(vocab, new_items, review_items, requested_review_words):
    today_iso = date.today().isoformat()

    for w in new_items:
        key = (w.get("german") or "").strip()
        if not key or key in vocab:
            continue
        vocab[key] = {
            "first_seen": today_iso,
            "review_count": 0,
            "next_review": compute_next_review_date(0),
        }

    returned_keys = {(w.get("german") or "").strip() for w in review_items}
    for key in requested_review_words:
        if key not in vocab:
            continue
        if key in returned_keys:
            vocab[key]["review_count"] = vocab[key].get("review_count", 0) + 1
            vocab[key]["last_reviewed"] = today_iso
            vocab[key]["next_review"] = compute_next_review_date(vocab[key]["review_count"])
        # if a requested review word wasn't returned, leave its next_review
        # alone so it simply comes up again tomorrow instead of being lost

    return vocab


# --------------------------------------------------------------------------- #
#  CLAUDE
# --------------------------------------------------------------------------- #
def generate_lesson(level_counts, review_words, known_words, grammar_topic):
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "temperature": TEMPERATURE,
        "system": SYSTEM_PROMPT,
        "messages": [{
            "role": "user",
            "content": build_user_message(level_counts, review_words, known_words, grammar_topic),
        }],
    }

    resp = requests.post(ANTHROPIC_URL, headers=headers, json=payload, timeout=120)
    resp.raise_for_status()
    data = resp.json()

    raw = "".join(block.get("text", "") for block in data.get("content", []))
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip()).strip()

    lesson = json.loads(raw)
    if "new_words" not in lesson:
        raise ValueError("Lesson JSON missing 'new_words'.")
    return lesson


# --------------------------------------------------------------------------- #
#  FORMATTING -> list of Telegram-safe HTML messages
# --------------------------------------------------------------------------- #
def esc(text):
    """Escape user/AI text for Telegram HTML parse mode."""
    return html.escape(str(text or ""), quote=False)


def format_word_block(tag_emoji, index, w):
    headword = (w.get("german") or "").strip()
    article = (w.get("article") or "").strip() or None
    # defensive: if the model put the article inside the word anyway, strip it
    if article and headword.lower().startswith(article.lower() + " "):
        headword = headword[len(article) + 1:]
    display = f"{article} {headword}" if article else headword

    header = f"<b>{tag_emoji} {index}. {esc(display)}</b>"
    plural = w.get("plural")
    if plural:
        header += f"  <i>(Pl. {esc(plural)})</i>"
    level = (w.get("level") or "").strip()
    if level:
        header += f"  [{esc(level)}]"

    lines = [header]
    synonyms = [s.strip() for s in (w.get("synonyms") or []) if s and s.strip()]
    if synonyms:
        lines.append(f"🔗 <i>{esc(', '.join(synonyms))}</i>")
    lines.append(f"➡️ {esc(w.get('english'))}")
    lines.append(f"🇹🇷 {esc(w.get('turkish'))}")
    if w.get("usage"):
        lines.append(f"💡 <i>{esc(w['usage'])}</i>")
        if w.get("usage_tr"):
            lines.append(f"🇹🇷 <i>{esc(w['usage_tr'])}</i>")

    family = w.get("family") or []
    fam_parts = []
    for fw in family:
        word = (fw.get("word") or "").strip()
        if not word:
            continue
        eng = (fw.get("english") or "").strip()
        tur = (fw.get("turkish") or "").strip()
        gloss = " / ".join(g for g in (eng, tur) if g)
        fam_parts.append(f"{esc(word)} ({esc(gloss)})" if gloss else esc(word))
    if fam_parts:
        lines.append(f"🌳 <i>{', '.join(fam_parts)}</i>")

    lines.append("")
    for ex in w.get("examples", []):
        lines.append(f"▪️ {esc(ex.get('de'))}")
        lines.append(f"   ↳ {esc(ex.get('en'))}")
        lines.append(f"   🇹🇷 {esc(ex.get('tr'))}")
    return "\n".join(lines)


def format_grammar_tip(tip):
    lines = ["📐 <b>Grammatik-Tipp</b>"]
    if tip.get("topic"):
        lines.append(f"<i>{esc(tip['topic'])}</i>")
    lines.append("")
    lines.append(esc(tip.get("explanation", "")))
    if tip.get("explanation_tr"):
        lines.append(f"🇹🇷 {esc(tip['explanation_tr'])}")
    examples = tip.get("examples", [])
    if examples:
        lines.append("")
        for ex in examples:
            lines.append(f"▪️ {esc(ex.get('de'))}")
            lines.append(f"   ↳ {esc(ex.get('en'))}")
            lines.append(f"   🇹🇷 {esc(ex.get('tr'))}")
    return "\n".join(lines)


def build_messages(lesson):
    header = "📚 <b>Deutsch des Tages — A2/B1</b> 🇬🇧🇹🇷\n\n"

    sections = []
    for i, w in enumerate(lesson.get("new_words", []), 1):
        sections.append(format_word_block("🆕", i, w))
    for i, w in enumerate(lesson.get("review_words", []), 1):
        sections.append(format_word_block("🔁", i, w))

    grammar_tip = lesson.get("grammar_tip")
    if grammar_tip:
        sections.append(format_grammar_tip(grammar_tip))

    messages = []
    current = header
    for section in sections:
        candidate = current + section + "\n\n"
        if len(candidate) > SAFE_CHUNK:
            messages.append(current.rstrip())
            current = section + "\n\n"
        else:
            current = candidate
    if current.strip():
        messages.append(current.rstrip())

    return messages


# --------------------------------------------------------------------------- #
#  VOICE  ->  German-only text extracted from the lesson, spoken via Edge TTS
# --------------------------------------------------------------------------- #
def collect_german_script(lesson):
    """Pull ONLY the German text out of a lesson (headwords + example
    sentences) for the voice note. English and Turkish fields are skipped
    entirely."""
    lines = []

    for w in lesson.get("new_words", []) + lesson.get("review_words", []):
        headword = (w.get("german") or "").strip()
        if not headword:
            continue
        article = (w.get("article") or "").strip() or None
        if article and headword.lower().startswith(article.lower() + " "):
            headword = headword[len(article) + 1:]
        display = f"{article} {headword}" if article else headword
        lines.append(f"{display}.")
        for ex in w.get("examples", []):
            de = (ex.get("de") or "").strip()
            if de:
                lines.append(de)

    for ex in (lesson.get("grammar_tip") or {}).get("examples", []):
        de = (ex.get("de") or "").strip()
        if de:
            lines.append(de)

    return "\n".join(lines)


async def _synthesize_mp3(text, mp3_path):
    communicate = edge_tts.Communicate(text, voice=TTS_VOICE)
    await communicate.save(mp3_path)


def synthesize_german_voice(text):
    """German text -> ogg/opus bytes (the format Telegram voice notes need),
    via Edge TTS + ffmpeg."""
    with tempfile.TemporaryDirectory() as tmp:
        mp3_path = os.path.join(tmp, "voice.mp3")
        ogg_path = os.path.join(tmp, "voice.ogg")

        asyncio.run(_synthesize_mp3(text, mp3_path))

        subprocess.run(
            ["ffmpeg", "-y", "-i", mp3_path,
             "-c:a", "libopus", "-b:a", "48k", "-ar", "48000", "-ac", "1",
             ogg_path],
            check=True, capture_output=True,
        )

        with open(ogg_path, "rb") as f:
            return f.read()


# --------------------------------------------------------------------------- #
#  TELEGRAM
# --------------------------------------------------------------------------- #
def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    resp = requests.post(url, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()


def send_telegram_voice(ogg_bytes):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendVoice"
    data = {"chat_id": TELEGRAM_CHAT_ID}
    files = {"voice": ("lesson.ogg", ogg_bytes, "audio/ogg")}
    resp = requests.post(url, data=data, files=files, timeout=60)
    resp.raise_for_status()
    return resp.json()


# --------------------------------------------------------------------------- #
#  ORCHESTRATION
# --------------------------------------------------------------------------- #
def run_once():
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID and ANTHROPIC_API_KEY):
        raise SystemExit(
            "Missing credentials. Set TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, "
            "and ANTHROPIC_API_KEY in your .env file (local) or GitHub Secrets (cloud)."
        )

    progress = load_progress()
    vocab = progress["vocab"]

    review_words = get_due_review_words(vocab, REVIEW_PER_DAY_TARGET)
    known_words = list(vocab.keys())
    grammar_topic = GRAMMAR_TOPICS[progress["grammar_index"] % len(GRAMMAR_TOPICS)]

    mix = ", ".join(f"{n}x{lvl}" for lvl, n in NEW_WORDS_PER_LEVEL.items() if n > 0)
    print(f"Requesting new words ({mix}), {len(review_words)} review word(s), "
          f"grammar topic: {grammar_topic}")
    lesson = generate_lesson(NEW_WORDS_PER_LEVEL, review_words, known_words, grammar_topic)

    # only trust review items that match a word we actually asked about
    review_items = [
        w for w in lesson.get("review_words", [])
        if (w.get("german") or "").strip() in review_words
    ]
    missing = set(review_words) - {(w.get("german") or "").strip() for w in review_items}
    if missing:
        print(f"Note: model did not return content for: {', '.join(missing)} "
              f"(will retry tomorrow)")

    lesson["review_words"] = review_items  # use the filtered/validated list for sending

    messages = build_messages(lesson)
    print(f"Sending {len(messages)} message(s) to Telegram...")
    for i, msg in enumerate(messages):
        send_telegram(msg)
        if i < len(messages) - 1:
            time.sleep(1)  # gentle pacing, avoids rate limits

    if ENABLE_VOICE:
        german_text = collect_german_script(lesson)
        if german_text.strip():
            print("Generating German voice note...")
            try:
                ogg_bytes = synthesize_german_voice(german_text)
                send_telegram_voice(ogg_bytes)
                print("Voice note sent.")
            except Exception as e:
                # Non-fatal: a TTS/ffmpeg hiccup shouldn't block the text
                # lesson that already sent successfully.
                print(f"Voice note failed, continuing without it: {e}")

    update_vocab_after_send(vocab, lesson.get("new_words", []), review_items, review_words)
    progress["grammar_index"] = (progress["grammar_index"] + 1) % len(GRAMMAR_TOPICS)
    save_progress(progress)

    print(f"Done. Vocab store now has {len(vocab)} word(s). "
          f"Next grammar topic: {GRAMMAR_TOPICS[progress['grammar_index']]}")


def run_scheduled():
    import schedule
    schedule.every().day.at(SEND_TIME).do(run_once)
    print(f"Scheduler running. Daily send at {SEND_TIME}. Ctrl+C to stop.")
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Daily German (A2/B1) reactivation Telegram bot")
    parser.add_argument("--once", action="store_true",
                        help="Generate and send a single lesson now, then exit.")
    args = parser.parse_args()

    if args.once:
        run_once()
    else:
        run_scheduled()
