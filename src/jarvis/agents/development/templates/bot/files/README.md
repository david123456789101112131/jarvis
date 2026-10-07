{{#if is_he}}
# {{ title }}

{{ description }}

בוט טלגרם שנבנה על ידי JARVIS.

## הפעלה
1. פותחים שיחה עם ‎@BotFather בטלגרם, שולחים `/newbot` ומקבלים טוקן.
2. מעתיקים את `.env.example` ל-`.env` ומדביקים את הטוקן אחרי `TELEGRAM_BOT_TOKEN=`.
   (הטוקן הוא סוד: לא משתפים אותו ולא מעלים אותו ל-Git.)
3. מתקינים ומפעילים:
```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

## פקודות
`/start`, `/help`, `/echo`, `/todo`, `/list`, `/done`, `/clear`, `/time`

## מבנה
- `bot/commands.py` — הלוגיקה (בלי תלות בטלגרם, עם בדיקות)
- `bot/telegram_adapter.py` — החיבור לטלגרם
- בדיקות: `python -m pytest -q`
{{else}}
# {{ title }}

{{ description }}

A Telegram bot built by JARVIS.

## Run it
1. Talk to @BotFather on Telegram, send `/newbot` and copy the token.
2. Copy `.env.example` to `.env` and paste the token after `TELEGRAM_BOT_TOKEN=`.
   (The token is a secret: don't share it or commit it to Git.)
3. Install and start:
```
python -m venv .venv
.venv\Scripts\activate        (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
python main.py
```

## Commands
`/start`, `/help`, `/echo`, `/todo`, `/list`, `/done`, `/clear`, `/time`

## Layout
- `bot/commands.py` — the logic (no Telegram dependency, tested)
- `bot/telegram_adapter.py` — the Telegram connection
- Tests: `python -m pytest -q`
{{/if}}
