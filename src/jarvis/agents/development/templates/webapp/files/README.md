{{#if is_he}}
# {{ brand }}

{{ description }}

אפליקציית ווב לניהול משימות שנבנתה על ידי JARVIS.

## איך פותחים
- פותחים את `index.html` בדפדפן. המשימות נשמרות בדפדפן (localStorage) ונשארות גם אחרי סגירה.

## מה יש
- הוספה, עריכה, סימון ומחיקה של משימות, עדיפויות ותאריכי יעד.
- מסננים (פתוחות, היום, באיחור, הושלמו), חיפוש ומיון, סטטיסטיקה והתקדמות.
- ייצוא וייבוא של המשימות לקובץ JSON, ערכת צבעים כהה ובהירה.
- קיצורי מקלדת: `N` משימה חדשה, `/` חיפוש, `Esc` ביטול.

## בדיקות
- `node --test` מריץ את בדיקות הלוגיקה (`js/tasks.js`).
{{else}}
# {{ brand }}

{{ description }}

A task manager web app built by JARVIS.

## Open it
- Open `index.html` in a browser. Tasks are saved in the browser (localStorage) and survive restarts.

## Features
- Add, edit, complete and delete tasks, with priorities and due dates.
- Filters (active, today, overdue, done), search and sorting, statistics and progress.
- Export and import tasks as JSON, dark and light themes.
- Keyboard shortcuts: `N` new task, `/` search, `Esc` cancel.

## Tests
- `node --test` runs the logic tests (`js/tasks.js`).
{{/if}}
