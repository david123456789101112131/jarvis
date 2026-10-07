{{#if is_he}}
# {{ title }}

{{ description }}

שרת REST ב-FastAPI שנבנה על ידי JARVIS.

## הפעלה
```
python -m venv .venv
.venv\Scripts\activate        (ב-macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
python -m uvicorn app.main:app --port 8000
```
ואז פותחים את http://127.0.0.1:8000/docs לתיעוד אינטראקטיבי.

## נקודות קצה
- `GET /health` — בדיקת תקינות
- `GET /items?q=&tag=&done=&limit=&offset=` — רשימה עם חיפוש ודפדוף
- `GET /items/{id}`, `POST /items`, `PUT /items/{id}`, `PATCH /items/{id}`, `DELETE /items/{id}`

הנתונים נשמרים ב-`data/items.json` (אפשר לשנות עם משתנה הסביבה `DATA_DIR`).

## בדיקות
- `python -m pytest -q`
{{else}}
# {{ title }}

{{ description }}

A FastAPI REST service built by JARVIS.

## Run it
```
python -m venv .venv
.venv\Scripts\activate        (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
python -m uvicorn app.main:app --port 8000
```
Then open http://127.0.0.1:8000/docs for the interactive docs.

## Endpoints
- `GET /health` — health check
- `GET /items?q=&tag=&done=&limit=&offset=` — list with search and paging
- `GET /items/{id}`, `POST /items`, `PUT /items/{id}`, `PATCH /items/{id}`, `DELETE /items/{id}`

Data is stored in `data/items.json` (change it with the `DATA_DIR` environment variable).

## Tests
- `python -m pytest -q`
{{/if}}
