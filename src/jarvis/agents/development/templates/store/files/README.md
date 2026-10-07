{{#if is_he}}
# {{ brand }}

{{ description }}

חנות אונליין מלאה שנבנתה על ידי JARVIS: קטלוג עם חיפוש, קטגוריות ומיון, עמודי מוצר, סל קניות שנשמר בדפדפן,
חישוב מע"מ ומשלוח, וטופס הזמנה עם בדיקות תקינות.

## איך פותחים
- פותחים את `index.html` בדפדפן (לחיצה כפולה). לא צריך שרת ולא צריך התקנות.

## איך משנים מוצרים
- כל המוצרים נמצאים בקובץ `data/products.js` — אפשר לשנות שם, מחיר, קטגוריה, תיאור, מלאי ותמונה.
- תמונות המוצרים נמצאות בתיקייה `images/products/`.
- הצבעים מוגדרים בראש `css/store.css` (משתני `--color-*`).

## בדיקות
- `node --test` (דורש Node.js 18 ומעלה) מריץ את הבדיקות של הסל, הקטלוג והתשלום.

## חשוב לדעת
- זו חנות הדגמה: אין חיוב אמיתי ואין שמירה בשרת. ההזמנות נשמרות רק ב-localStorage של הדפדפן.
- כדי לקבל תשלומים אמיתיים צריך לחבר ספק סליקה (למשל Stripe, PayPal או ספק ישראלי) ושרת.

## מבנה
- `index.html` — הקטלוג · `product.html` — עמוד מוצר · `cart.html` — סל · `checkout.html` — הזמנה
- `js/cart.js`, `js/catalog.js`, `js/checkout.js` — הלוגיקה (פונקציות טהורות עם בדיקות)
- `js/app.js` — חיבור הלוגיקה לעמודים
{{else}}
# {{ brand }}

{{ description }}

A complete online store built by JARVIS: a catalog with search, categories and sorting, product pages, a cart saved
in the browser, tax and shipping, and a validated checkout form.

## Open it
- Open `index.html` in a browser (double-click). No server or installation needed.

## Change the products
- All products live in `data/products.js`: name, price, category, description, stock and image.
- Product images are in `images/products/`.
- Colors are defined at the top of `css/store.css` (the `--color-*` variables).

## Tests
- `node --test` (Node.js 18+) runs the cart, catalog and checkout tests.

## Good to know
- This is a demo store: nothing is charged and nothing is stored on a server. Orders are kept in the browser's
  localStorage only.
- Taking real payments needs a payment provider (for example Stripe or PayPal) and a server.

## Structure
- `index.html` (catalog) · `product.html` (product page) · `cart.html` (cart) · `checkout.html` (checkout)
- `js/cart.js`, `js/catalog.js`, `js/checkout.js` — the logic (pure functions with tests)
- `js/app.js` — wires the logic to the pages
{{/if}}
