{{#if is_he}}
# {{ title }}

{{ description }}

שרת REST ב-Node.js שנבנה על ידי JARVIS, בלי תלויות חיצוניות.

## הפעלה
- `node server.js` (או `npm start`) — השרת עולה בכתובת http://127.0.0.1:3000
- משנים פורט עם משתנה הסביבה `PORT`.

## נקודות קצה
- `GET /health`
- `GET /api/items?q=` · `POST /api/items`
- `GET /api/items/:id` · `PUT /api/items/:id` · `PATCH /api/items/:id` · `DELETE /api/items/:id`

הנתונים נשמרים ב-`data/items.json`.

## בדיקות
- `node --test` (או `npm test`)
{{else}}
# {{ title }}

{{ description }}

A dependency-free Node.js REST API built by JARVIS.

## Run it
- `node server.js` (or `npm start`) — the server listens on http://127.0.0.1:3000
- Change the port with the `PORT` environment variable.

## Endpoints
- `GET /health`
- `GET /api/items?q=` · `POST /api/items`
- `GET /api/items/:id` · `PUT /api/items/:id` · `PATCH /api/items/:id` · `DELETE /api/items/:id`

Data is stored in `data/items.json`.

## Tests
- `node --test` (or `npm test`)
{{/if}}
