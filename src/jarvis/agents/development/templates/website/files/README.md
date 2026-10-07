{{#if is_he}}
# {{ brand }}

{{ description }}

אתר שנבנה על ידי JARVIS: עמוד בית, אודות, שירותים וצור קשר, מותאם לנייד ולקוראי מסך.

## איך פותחים
- פותחים את `index.html` בדפדפן (לחיצה כפולה).

## מה אפשר לשנות
- טקסטים: ישירות בקבצי ה-HTML.
- צבעים: משתני `--color-*` בראש `css/site.css`.
- הטופס בעמוד צור קשר בודק את השדות, שומר את ההודעה בדפדפן ומכין מייל ל-{{ email }}. כדי לקבל הודעות
  ישירות לשרת אפשר לחבר שירות טפסים (למשל Formspree) או שרת משלכם.

## בדיקות
- `node --test` מריץ את בדיקות הטופס (Node.js 18 ומעלה).
{{else}}
# {{ brand }}

{{ description }}

A website built by JARVIS: home, about, services and contact pages, responsive and screen-reader friendly.

## Open it
- Open `index.html` in a browser (double-click).

## What to change
- Texts: directly in the HTML files.
- Colors: the `--color-*` variables at the top of `css/site.css`.
- The contact form validates the fields, keeps the message in the browser and prepares an email to {{ email }}.
  To receive messages on a server, connect a form service (for example Formspree) or your own backend.

## Tests
- `node --test` runs the form tests (Node.js 18+).
{{/if}}
