{{#if is_he}}
# {{ title }}

{{ description }}

כלי שורת פקודה בפייתון שנבנה על ידי JARVIS.

## שימוש
```
python main.py stats "C:\Users\me\Downloads"      סטטיסטיקה של תיקייה
python main.py dupes "C:\Users\me\Pictures"       קבצים כפולים
python main.py words notes.txt --top 20          המילים הנפוצות בקובץ
```
אפשר להוסיף `--json` לכל פקודה.

## מבנה
- `core.py` — הלוגיקה (פונקציות שקל לבדוק ולהרחיב)
- `main.py` — שורת הפקודה (argparse)
- `tests/` — בדיקות: `python -m pytest -q`
{{else}}
# {{ title }}

{{ description }}

A Python command-line tool built by JARVIS.

## Usage
```
python main.py stats ~/Downloads        folder statistics
python main.py dupes ~/Pictures         duplicate files
python main.py words notes.txt --top 20 most common words
```
Add `--json` to any command for machine-readable output.

## Layout
- `core.py` — the logic (plain functions, easy to test and extend)
- `main.py` — the command line (argparse)
- `tests/` — tests: `python -m pytest -q`
{{/if}}
