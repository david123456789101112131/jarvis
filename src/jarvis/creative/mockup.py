"""UI mockups: polished, responsive HTML screens plus low-fi SVG wireframes.

`generate_mockup("אפליקציה לבית קפה")` returns ready-to-open pages (landing, dashboard, login,
product, settings and a mobile app screen) that use the brief's palette, icons and copy, an
index page that links them, and one wireframe per screen. Everything is offline and
deterministic for the same brief.
"""

from __future__ import annotations

import random
from collections.abc import Sequence

from ._util import FONT_STACK, esc, make_rng, resolve_lang, text_dir
from .assets import Asset
from .domains import Domain, detect_domain
from .icons import icon_svg
from .logo import initials
from .palette import Palette, generate_palette
from .texts import landing_copy

SCREENS: tuple[str, ...] = ("landing", "dashboard", "login", "product", "settings", "mobile")

_LABELS = {
    "landing": ("דף נחיתה", "Landing page"), "dashboard": ("לוח בקרה", "Dashboard"),
    "login": ("התחברות", "Sign in"), "product": ("עמוד מוצר", "Product page"),
    "settings": ("הגדרות", "Settings"), "mobile": ("אפליקציה לנייד", "Mobile app"),
}
_UI = {
    "he": {
        "nav": ("בית", "שירותים", "אודות", "צור קשר"), "login": "התחברות", "features": "למה דווקא אנחנו",
        "testimonials": "מה הלקוחות אומרים", "ready": "מוכנים להתחיל?", "rights": "כל הזכויות שמורות",
        "search": "חיפוש…", "overview": "סקירה כללית", "recent": "פעילות אחרונה", "orders": "הזמנות אחרונות",
        "menu": ("ראשי", "הזמנות", "מוצרים", "לקוחות", "דוחות", "הגדרות"), "item": "פריט", "customer": "לקוח",
        "amount": "סכום", "status": "סטטוס", "statuses": ("הושלם", "בטיפול", "נשלח", "חדש"), "week": "השבוע",
        "welcome": "ברוכים השבים", "email": "אימייל", "password": "סיסמה", "remember": "זכור אותי",
        "forgot": "שכחת סיסמה?", "signin": "כניסה", "no_account": "אין לך חשבון?", "signup": "הרשמה",
        "add_cart": "הוספה לסל", "qty": "כמות", "reviews": "ביקורות", "related": "אולי יעניין אותך",
        "in_stock": "במלאי · משלוח תוך 2–4 ימי עסקים", "tabs": ("פרופיל", "התראות", "אבטחה", "חיוב"),
        "name": "שם מלא", "phone": "טלפון", "language": "שפה", "save": "שמירת שינויים", "cancel": "ביטול",
        "notify": ("עדכונים במייל", "התראות פוש", "סיכום שבועי"), "two_factor": "אימות דו-שלבי",
        "hello": "שלום, נועה", "today": "מה נעשה היום?", "popular": "פופולרי עכשיו", "see_all": "הכול",
        "tabbar": ("בית", "חיפוש", "סל", "פרופיל"), "screens": "מסכים", "wireframe": "סקיצה",
        "open": "פתיחה", "index_title": "אב-טיפוס", "index_sub": "מסכי עיצוב לחיצים — לחצו על מסך כדי לפתוח אותו",
        "lang_name": "עברית", "member_since": "חבר/ה מאז 2024", "ago": "לפני {n} דק׳",
    },
    "en": {
        "nav": ("Home", "Services", "About", "Contact"), "login": "Sign in", "features": "Why choose us",
        "testimonials": "What customers say", "ready": "Ready to get started?", "rights": "All rights reserved",
        "search": "Search…", "overview": "Overview", "recent": "Recent activity", "orders": "Recent orders",
        "menu": ("Home", "Orders", "Products", "Customers", "Reports", "Settings"), "item": "Item",
        "customer": "Customer", "amount": "Amount", "status": "Status",
        "statuses": ("Completed", "Processing", "Shipped", "New"), "week": "This week",
        "welcome": "Welcome back", "email": "Email", "password": "Password", "remember": "Remember me",
        "forgot": "Forgot password?", "signin": "Sign in", "no_account": "No account yet?", "signup": "Sign up",
        "add_cart": "Add to cart", "qty": "Qty", "reviews": "reviews", "related": "You may also like",
        "in_stock": "In stock · ships in 2–4 business days", "tabs": ("Profile", "Notifications", "Security", "Billing"),
        "name": "Full name", "phone": "Phone", "language": "Language", "save": "Save changes", "cancel": "Cancel",
        "notify": ("Email updates", "Push notifications", "Weekly summary"), "two_factor": "Two-factor authentication",
        "hello": "Hi, Noa", "today": "What would you like today?", "popular": "Popular now", "see_all": "See all",
        "tabbar": ("Home", "Search", "Cart", "Profile"), "screens": "Screens", "wireframe": "Wireframe",
        "open": "Open", "index_title": "Prototype", "index_sub": "Clickable design screens — pick one to open it",
        "lang_name": "English", "member_since": "Member since 2024", "ago": "{n} min ago",
    },
}
_PEOPLE = {"he": ("נועה כהן", "דניאל לוי", "מיכל אברהם", "יוסי מזרחי", "שירה פרץ"),
           "en": ("Noa Cohen", "Daniel Levy", "Michal Abraham", "Yossi Mizrahi", "Shira Peretz")}

_BASE_CSS = """
*,*::before,*::after{box-sizing:border-box}
html,body{margin:0}
body{font-family:{font};background:var(--color-background);color:var(--color-text);line-height:1.6;
  -webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none}
img,svg{display:block}
.wrap{width:min(1120px,100% - 2.5rem);margin-inline:auto}
.btn{display:inline-flex;align-items:center;gap:.5rem;border:0;border-radius:999px;padding:.8rem 1.4rem;
  font:inherit;font-weight:700;cursor:pointer;transition:transform .15s ease,box-shadow .15s ease}
.btn:hover{transform:translateY(-1px)}
.btn-primary{background:var(--color-primary);color:var(--color-on-primary);
  box-shadow:0 8px 24px rgba(var(--color-primary-rgb),.28)}
.btn-ghost{background:transparent;color:var(--color-text);border:1.5px solid var(--color-border)}
.card{background:var(--color-surface);border:1px solid var(--color-border);border-radius:18px;
  box-shadow:0 1px 2px rgba(0,0,0,.04),0 12px 32px rgba(0,0,0,.05)}
.muted{color:var(--color-muted)}
.badge{display:inline-flex;align-items:center;gap:.35rem;padding:.25rem .7rem;border-radius:999px;font-size:.8rem;
  font-weight:700;background:rgba(var(--color-accent-rgb),.16);color:var(--color-text)}
.brand{display:flex;align-items:center;gap:.6rem;font-weight:800;font-size:1.1rem}
.brand-mark{width:2.2rem;height:2.2rem;border-radius:12px;display:grid;place-items:center;font-weight:800;
  background:linear-gradient(135deg,var(--color-primary),var(--color-secondary));color:var(--color-on-primary)}
.icon-tile{width:3rem;height:3rem;border-radius:14px;display:grid;place-items:center;
  background:rgba(var(--color-primary-rgb),.1);color:var(--color-primary)}
input,select{font:inherit;color:inherit;width:100%;padding:.75rem .9rem;border-radius:12px;
  border:1.5px solid var(--color-border);background:var(--color-surface)}
input:focus,select:focus{outline:2px solid rgba(var(--color-primary-rgb),.35);border-color:var(--color-primary)}
label{display:grid;gap:.35rem;font-weight:600;font-size:.92rem}
.toggle{width:2.8rem;height:1.6rem;border-radius:999px;background:var(--color-border);position:relative;flex:none}
.toggle::after{content:"";position:absolute;top:.2rem;inset-inline-start:.2rem;width:1.2rem;height:1.2rem;
  border-radius:50%;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.25)}
.toggle.on{background:var(--color-primary)}
.toggle.on::after{inset-inline-start:auto;inset-inline-end:.2rem}
"""

_SCREEN_CSS = {
    "landing": """
header.top{position:sticky;top:0;z-index:5;backdrop-filter:blur(12px);
  background:rgba(var(--color-background-rgb),.85);border-bottom:1px solid var(--color-border)}
header.top .wrap{display:flex;align-items:center;justify-content:space-between;height:4.25rem;gap:1rem}
nav.links{display:flex;gap:1.6rem;font-weight:600}
.hero{padding:5.5rem 0 4rem;background:radial-gradient(60rem 30rem at 85% -10%,rgba(var(--color-accent-rgb),.22),
  transparent),radial-gradient(40rem 26rem at 0% 0%,rgba(var(--color-primary-rgb),.14),transparent)}
.hero .wrap{display:grid;grid-template-columns:1.15fr .85fr;gap:3rem;align-items:center}
.hero h1{font-size:clamp(2.1rem,4.5vw,3.6rem);line-height:1.12;margin:.8rem 0 1rem;letter-spacing:-.02em}
.hero p.lead{font-size:1.2rem;max-width:34rem}
.hero .actions{display:flex;gap:.8rem;flex-wrap:wrap;margin-top:1.8rem}
.hero-art{aspect-ratio:1;border-radius:32px;display:grid;place-items:center;color:var(--color-on-primary);
  background:linear-gradient(140deg,var(--color-primary),var(--color-secondary) 60%,var(--color-accent));
  box-shadow:0 30px 80px rgba(var(--color-primary-rgb),.35)}
section{padding:4.5rem 0}
h2{font-size:2rem;margin:0 0 2rem;text-align:center}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:1.25rem}
.feature{padding:1.6rem}
.feature h3{margin:1rem 0 .4rem}
.quote{padding:1.6rem}
.quote p{font-size:1.05rem;margin:0 0 1rem}
.cta-band{margin:0 auto 4.5rem;padding:3rem;border-radius:28px;text-align:center;color:var(--color-on-primary);
  background:linear-gradient(120deg,var(--color-primary),var(--color-secondary))}
.cta-band .btn{background:#fff;color:var(--color-primary)}
footer{border-top:1px solid var(--color-border);padding:2rem 0;font-size:.9rem}
footer .wrap{display:flex;justify-content:space-between;gap:1rem;flex-wrap:wrap}
@media (max-width:820px){nav.links{display:none}.hero .wrap{grid-template-columns:1fr}.hero-art{max-width:320px}}
""",
    "dashboard": """
.layout{display:grid;grid-template-columns:250px 1fr;min-height:100vh}
aside{background:var(--color-surface);border-inline-end:1px solid var(--color-border);padding:1.4rem 1rem}
aside .brand{margin:0 .5rem 2rem}
aside a{display:flex;align-items:center;gap:.75rem;padding:.7rem .9rem;border-radius:12px;font-weight:600;
  color:var(--color-muted)}
aside a.active{background:rgba(var(--color-primary-rgb),.1);color:var(--color-primary)}
main{padding:1.6rem 2rem;display:grid;gap:1.5rem;align-content:start}
.topbar{display:flex;align-items:center;justify-content:space-between;gap:1rem}
.search{display:flex;align-items:center;gap:.5rem;padding:.6rem 1rem;border-radius:999px;width:min(360px,100%);
  background:var(--color-surface);border:1px solid var(--color-border);color:var(--color-muted)}
.avatar{width:2.5rem;height:2.5rem;border-radius:50%;display:grid;place-items:center;font-weight:800;
  background:var(--color-accent);color:var(--color-text)}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:1rem}
.kpi{padding:1.2rem 1.3rem}
.kpi .value{font-size:1.8rem;font-weight:800;margin:.3rem 0}
.delta{font-weight:700;font-size:.85rem;color:#15803d}
.delta.down{color:#b91c1c}
.two{display:grid;grid-template-columns:1.6fr 1fr;gap:1rem}
.panel{padding:1.3rem 1.4rem}
.panel h3{margin:0 0 1rem;display:flex;justify-content:space-between;align-items:center}
table{width:100%;border-collapse:collapse;font-size:.93rem}
th,td{text-align:start;padding:.75rem .5rem;border-bottom:1px solid var(--color-border)}
th{color:var(--color-muted);font-weight:600}
.pill{padding:.2rem .65rem;border-radius:999px;font-size:.78rem;font-weight:700;
  background:rgba(var(--color-primary-rgb),.1);color:var(--color-primary)}
.feed{display:grid;gap:.9rem}
.feed div{display:flex;gap:.75rem;align-items:flex-start}
@media (max-width:1000px){.layout{grid-template-columns:1fr}aside{display:none}.kpis{grid-template-columns:repeat(2,1fr)}
  .two{grid-template-columns:1fr}}
""",
    "login": """
.split{display:grid;grid-template-columns:1fr 1fr;min-height:100vh}
.side{padding:3rem;display:flex;flex-direction:column;justify-content:space-between;color:var(--color-on-primary);
  background:radial-gradient(30rem 20rem at 20% 20%,rgba(255,255,255,.18),transparent),
  linear-gradient(150deg,var(--color-primary),var(--color-secondary))}
.side .brand-mark{background:rgba(255,255,255,.2)}
.side h1{font-size:2.6rem;line-height:1.15;margin:0 0 1rem}
.form-side{display:grid;place-items:center;padding:2rem}
form{width:min(400px,100%);display:grid;gap:1.1rem}
form h2{margin:0;font-size:1.8rem}
.row{display:flex;justify-content:space-between;align-items:center;font-size:.9rem}
.row label{display:flex;align-items:center;gap:.5rem;font-weight:500}
.row input{width:auto}
.link{color:var(--color-primary);font-weight:700}
.btn-primary{justify-content:center;width:100%;border-radius:12px}
@media (max-width:820px){.split{grid-template-columns:1fr}.side{display:none}}
""",
    "product": """
header.top{border-bottom:1px solid var(--color-border)}
header.top .wrap{display:flex;justify-content:space-between;align-items:center;height:4rem}
.crumbs{font-size:.88rem;margin:1.5rem 0}
.product{display:grid;grid-template-columns:1fr 1fr;gap:3rem;align-items:start}
.gallery{display:grid;gap:.8rem}
.shot{aspect-ratio:4/3;border-radius:24px;display:grid;place-items:center;color:var(--color-on-primary);
  background:linear-gradient(135deg,var(--color-primary),var(--color-secondary))}
.thumbs{display:grid;grid-template-columns:repeat(4,1fr);gap:.6rem}
.thumbs div{aspect-ratio:1;border-radius:14px;background:rgba(var(--color-primary-rgb),.12)}
.thumbs div:first-child{outline:2px solid var(--color-primary)}
h1{font-size:2.2rem;margin:.5rem 0}
.price{font-size:1.8rem;font-weight:800;color:var(--color-primary)}
.stars{display:flex;gap:.15rem;align-items:center;color:var(--color-accent)}
.buy{display:flex;gap:.8rem;align-items:center;margin:1.5rem 0}
.qty{display:flex;align-items:center;border:1.5px solid var(--color-border);border-radius:999px}
.qty span{padding:.6rem 1rem;font-weight:700}
ul.checks{list-style:none;padding:0;display:grid;gap:.55rem}
ul.checks li{display:flex;gap:.55rem;align-items:center}
.related{margin:4rem 0}
.related .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:1rem}
.mini{padding:1rem}
.mini .img{aspect-ratio:1;border-radius:14px;display:grid;place-items:center;margin-bottom:.8rem;
  background:rgba(var(--color-primary-rgb),.1);color:var(--color-primary)}
@media (max-width:820px){.product{grid-template-columns:1fr}}
""",
    "settings": """
.page{width:min(980px,100% - 2rem);margin:2.5rem auto}
.head{display:flex;align-items:center;gap:1rem;margin-bottom:1.6rem}
.head .avatar{width:4rem;height:4rem;border-radius:50%;display:grid;place-items:center;font-size:1.4rem;font-weight:800;
  background:linear-gradient(135deg,var(--color-primary),var(--color-accent));color:var(--color-on-primary)}
.tabs{display:flex;gap:.4rem;border-bottom:1px solid var(--color-border);margin-bottom:1.5rem;overflow-x:auto}
.tabs a{padding:.8rem 1.1rem;font-weight:600;color:var(--color-muted);border-bottom:2.5px solid transparent}
.tabs a.active{color:var(--color-primary);border-color:var(--color-primary)}
.section{padding:1.6rem;display:grid;gap:1.2rem;margin-bottom:1.2rem}
.section h3{margin:0}
.fields{display:grid;grid-template-columns:1fr 1fr;gap:1rem}
.switch-row{display:flex;justify-content:space-between;align-items:center;gap:1rem;padding:.4rem 0}
.actions{display:flex;gap:.8rem;justify-content:flex-end}
@media (max-width:700px){.fields{grid-template-columns:1fr}}
""",
    "mobile": """
body{display:grid;place-items:center;min-height:100vh;padding:2rem;
  background:radial-gradient(50rem 30rem at 50% 0%,rgba(var(--color-primary-rgb),.18),transparent),var(--color-background)}
.phone{width:375px;height:780px;border-radius:48px;padding:14px;background:#0f0f12;
  box-shadow:0 40px 90px rgba(0,0,0,.35)}
.screen{position:relative;height:100%;border-radius:36px;overflow:hidden;background:var(--color-background);
  display:flex;flex-direction:column}
.status{display:flex;justify-content:space-between;padding:.9rem 1.6rem .3rem;font-size:.8rem;font-weight:700}
.content{flex:1;overflow:auto;padding:.6rem 1.2rem 1rem;display:grid;gap:1.1rem;align-content:start}
.hello h2{margin:0;font-size:1.45rem}
.search{display:flex;gap:.5rem;align-items:center;padding:.7rem 1rem;border-radius:16px;background:var(--color-surface);
  border:1px solid var(--color-border);color:var(--color-muted)}
.promo{border-radius:22px;padding:1.2rem;color:var(--color-on-primary);display:flex;justify-content:space-between;
  align-items:center;background:linear-gradient(130deg,var(--color-primary),var(--color-secondary))}
.chips{display:flex;flex-wrap:wrap;gap:.5rem}
.chips span{white-space:nowrap;padding:.45rem .9rem;border-radius:999px;background:var(--color-surface);
  border:1px solid var(--color-border);font-size:.85rem;font-weight:600}
.chips span:first-child{background:var(--color-primary);color:var(--color-on-primary);border-color:transparent}
.list{display:grid;gap:.7rem}
.item{display:flex;align-items:center;gap:.8rem;padding:.7rem;border-radius:18px;background:var(--color-surface);
  border:1px solid var(--color-border)}
.item .img{width:3.4rem;height:3.4rem;border-radius:14px;display:grid;place-items:center;flex:none;
  background:rgba(var(--color-primary-rgb),.12);color:var(--color-primary)}
.item .grow{flex:1;min-width:0}
.item b{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.plus{width:2rem;height:2rem;border-radius:50%;display:grid;place-items:center;background:var(--color-primary);
  color:var(--color-on-primary)}
.tabbar{display:grid;grid-template-columns:repeat(4,1fr);padding:.6rem .8rem 1.1rem;background:var(--color-surface);
  border-top:1px solid var(--color-border);font-size:.72rem;text-align:center}
.tabbar a{display:grid;justify-items:center;gap:.2rem;color:var(--color-muted)}
.tabbar a.active{color:var(--color-primary);font-weight:700}
.row-head{display:flex;justify-content:space-between;align-items:baseline}
""",
    "index": """
.page{width:min(1180px,100% - 2rem);margin:3rem auto}
.page header{display:flex;align-items:center;gap:1rem;margin-bottom:2rem}
.page h1{margin:0}
.screens{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:1.2rem}
.screens a.card{display:grid;overflow:hidden;transition:transform .15s ease}
.screens a.card:hover{transform:translateY(-3px)}
.thumb{background:rgba(var(--color-primary-rgb),.06);border-bottom:1px solid var(--color-border);padding:1rem;
  display:grid;place-items:center}
.thumb svg{width:100%;height:auto;max-height:220px}
.meta{padding:1rem 1.2rem;display:flex;justify-content:space-between;align-items:center}
.swatches{display:flex;gap:.4rem}
.swatches span{width:1.4rem;height:1.4rem;border-radius:50%;border:1px solid rgba(0,0,0,.08)}
""",
}


def _page(title: str, body: str, palette: Palette, lang: str, screen: str) -> str:
    css = palette.to_css() + _BASE_CSS.replace("{font}", FONT_STACK) + _SCREEN_CSS.get(screen, "")
    return (f'<!doctype html>\n<html lang="{lang}" dir="{text_dir(lang)}">\n<head>\n<meta charset="utf-8">\n'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">\n<title>{esc(title)}</title>\n'
            f"<style>{css}</style>\n</head>\n<body>\n{body}\n</body>\n</html>\n")


def _ico(name: str, size: int = 22, color: str = "currentColor") -> str:
    return icon_svg(name, size=size, color=color)


def _money(value: float, lang: str) -> str:
    return f"₪{value:,.0f}" if lang == "he" else f"${value:,.2f}"


_RATING_WORDS = ("דירוג", "rating", "stars", "כוכבים")
_PERCENT_WORDS = ("שביעות", "חוזרים", "אחוז", "המרה", "satisfaction", "returning", "rate", "retention", "conversion",
                  "%")
_MONEY_WORDS = ("הכנסות", "מכירות", "רווח", "revenue", "sales", "income", "profit", "₪", "$")


def _kpi_value(label: str, lang: str, rng: random.Random) -> str:
    low = label.lower()
    if any(w in low for w in _RATING_WORDS):
        return f"{rng.randint(42, 50) / 10:.1f}"
    if any(w in low for w in _PERCENT_WORDS):
        return f"{rng.randint(55, 98)}%"
    if any(w in low for w in _MONEY_WORDS):
        return _money(rng.randint(8, 95) * 1000, lang)
    return f"{rng.randint(24, 480):,}"


class _Ctx:
    def __init__(self, brief: str, palette: Palette, lang: str) -> None:
        self.brief, self.palette, self.lang = brief, palette, lang
        self.ui = _UI[lang]
        self.copy = landing_copy(brief or "app", lang=lang)
        self.domain: Domain = detect_domain(brief)
        self.name = str(self.copy.get("name") or brief or "Studio")
        self.mark = esc(initials(self.name) or self.name[:1])
        self.rng = make_rng("mockup", brief, lang)
        self.icons = list(self.domain.icons) or ["star", "heart", "home", "user"]

    def brand(self) -> str:
        return f'<a class="brand" href="#"><span class="brand-mark">{self.mark}</span>{esc(self.name)}</a>'

    def products(self) -> list[tuple[str, str, float, str, str]]:
        """(name, category, price, description, icon) for the brief's domain."""
        lang = self.lang
        out = [(p.name[lang], p.category[lang], float(p.price[lang]), p.description[lang], p.icon)
               for p in self.domain.products]
        if not out:
            from .domains import GENERIC

            out = [(p.name[lang], p.category[lang], float(p.price[lang]), p.description[lang], p.icon)
                   for p in GENERIC.products]
        return out


# ------------------------------------------------------------------------------------------ screens
def _landing(c: _Ctx) -> str:
    ui, copy = c.ui, c.copy
    links = "".join(f'<a href="#">{esc(x)}</a>' for x in ui["nav"])
    features = "".join(
        f'<article class="card feature"><div class="icon-tile">{_ico(str(f["icon"]))}</div>'
        f'<h3>{esc(f["title"])}</h3><p class="muted">{esc(f["text"])}</p></article>'
        for f in copy["features"]  # type: ignore[union-attr]
    )
    quotes = "".join(
        f'<figure class="card quote"><p>“{esc(q["quote"])}”</p><figcaption><b>{esc(q["name"])}</b>'
        f'<div class="muted">{esc(q["role"])}</div></figcaption></figure>'
        for q in copy["testimonials"]  # type: ignore[union-attr]
    )
    hero_icon = c.icons[0] if c.icons else "star"
    return f"""<header class="top"><div class="wrap">{c.brand()}<nav class="links">{links}</nav>
<a class="btn btn-primary" href="#">{esc(ui["login"])}</a></div></header>
<section class="hero"><div class="wrap"><div><span class="badge">{esc(copy["emoji"])} {esc(copy["label"])}</span>
<h1>{esc(copy["headline"])}</h1><p class="lead muted">{esc(copy["subheadline"])}</p>
<div class="actions"><a class="btn btn-primary" href="#">{esc(copy["cta"])}</a>
<a class="btn btn-ghost" href="#">{esc(copy["secondary_cta"])}</a></div></div>
<div class="hero-art">{_ico(hero_icon, 150, "#ffffff")}</div></div></section>
<section><div class="wrap"><h2>{esc(ui["features"])}</h2><div class="grid">{features}</div></div></section>
<section><div class="wrap"><h2>{esc(ui["testimonials"])}</h2><div class="grid">{quotes}</div></div></section>
<div class="wrap"><div class="cta-band"><h2 style="color:inherit;margin-bottom:.6rem">{esc(ui["ready"])}</h2>
<p>{esc(copy["about"])}</p><a class="btn" href="#">{esc(copy["cta"])}</a></div></div>
<footer><div class="wrap"><span>© 2026 {esc(c.name)} · {esc(ui["rights"])}</span><span class="muted">{links}</span>
</div></footer>"""


def _chart_svg(c: _Ctx) -> str:
    values = [c.rng.randint(35, 100) for _ in range(12)]
    w, h, pad = 560, 200, 10
    step = (w - pad * 2) / (len(values) - 1)
    pts = [(pad + i * step, h - pad - (v / 100) * (h - pad * 2)) for i, v in enumerate(values)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = f"{pad},{h - pad} {line} {w - pad},{h - pad}"
    grid = "".join(f'<line x1="0" x2="{w}" y1="{y}" y2="{y}" stroke="var(--color-border)" />'
                   for y in range(pad, h, 45))
    dots = "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="var(--color-primary)" />' for x, y in pts[-1:])
    return (f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="chart">'
            '<defs><linearGradient id="g" x1="0" x2="0" y1="0" y2="1">'
            '<stop offset="0" stop-color="var(--color-primary)" stop-opacity=".28"/>'
            '<stop offset="1" stop-color="var(--color-primary)" stop-opacity="0"/></linearGradient></defs>'
            f'{grid}<polygon points="{area}" fill="url(#g)"/>'
            f'<polyline points="{line}" fill="none" stroke="var(--color-primary)" stroke-width="3" '
            f'stroke-linejoin="round" stroke-linecap="round"/>{dots}</svg>')


def _dashboard(c: _Ctx) -> str:
    ui, lang, rng = c.ui, c.lang, c.rng
    menu_icons = ("home", "cart", "package", "users", "chart", "settings")
    menu = "".join(f'<a href="#" class="{"active" if i == 0 else ""}">{_ico(ic, 20)}{esc(label)}</a>'
                   for i, (ic, label) in enumerate(zip(menu_icons, ui["menu"], strict=False)))
    kpis_html = []
    for label in c.domain.kpis.get(lang, ())[:4]:
        value = _kpi_value(label, lang, rng)
        delta = rng.randint(-6, 18)
        sign = "down" if delta < 0 else ""
        kpis_html.append(f'<div class="card kpi"><div class="muted">{esc(label)}</div><div class="value">{value}</div>'
                         f'<span class="delta {sign}">{"▼" if delta < 0 else "▲"} {abs(delta)}%</span></div>')
    people = _PEOPLE[lang]
    rows = "".join(
        f"<tr><td>{esc(p[0])}</td><td>{esc(people[i % len(people)])}</td><td>{_money(p[2] * rng.randint(1, 3), lang)}</td>"
        f'<td><span class="pill">{esc(ui["statuses"][i % 4])}</span></td></tr>'
        for i, p in enumerate(c.products()[:5])
    )
    feed = "".join(
        f'<div><span class="icon-tile" style="width:2.2rem;height:2.2rem">{_ico(c.icons[i % len(c.icons)], 16)}</span>'
        f'<span><b>{esc(people[(i + 2) % len(people)])}</b> · {esc(p[0])}<br>'
        f'<small class="muted">{ui["ago"].format(n=rng.randint(2, 59))}</small></span></div>'
        for i, p in enumerate(c.products()[:4])
    )
    return f"""<div class="layout"><aside>{c.brand()}<nav>{menu}</nav></aside>
<main><div class="topbar"><h1 style="margin:0;font-size:1.5rem">{esc(ui["overview"])}</h1>
<div class="search">{_ico("search", 18)}{esc(ui["search"])}</div>
<div style="display:flex;gap:.8rem;align-items:center">{_ico("bell")}<span class="avatar">{esc(people[0][:1])}</span></div></div>
<div class="kpis">{"".join(kpis_html)}</div>
<div class="two"><div class="card panel"><h3>{esc(ui["week"])} <span class="badge">+12%</span></h3>{_chart_svg(c)}</div>
<div class="card panel"><h3>{esc(ui["recent"])}</h3><div class="feed">{feed}</div></div></div>
<div class="card panel"><h3>{esc(ui["orders"])}</h3><table><thead><tr><th>{esc(ui["item"])}</th>
<th>{esc(ui["customer"])}</th><th>{esc(ui["amount"])}</th><th>{esc(ui["status"])}</th></tr></thead>
<tbody>{rows}</tbody></table></div></main></div>"""


def _login(c: _Ctx) -> str:
    ui, copy = c.ui, c.copy
    return f"""<div class="split"><div class="side">{c.brand()}<div><h1>{esc(copy["headline"])}</h1>
<p>{esc(copy["subheadline"])}</p></div><small>© 2026 {esc(c.name)}</small></div>
<div class="form-side"><form onsubmit="return false"><h2>{esc(ui["welcome"])}</h2>
<p class="muted" style="margin:0">{esc(copy["offer"])}</p>
<label>{esc(ui["email"])}<input type="email" placeholder="name@example.com" autocomplete="email"></label>
<label>{esc(ui["password"])}<input type="password" placeholder="••••••••" autocomplete="current-password"></label>
<div class="row"><label><input type="checkbox" checked> {esc(ui["remember"])}</label>
<a class="link" href="#">{esc(ui["forgot"])}</a></div>
<button class="btn btn-primary" type="submit">{esc(ui["signin"])}</button>
<p class="muted" style="text-align:center;margin:0">{esc(ui["no_account"])} <a class="link" href="#">{esc(ui["signup"])}</a></p>
</form></div></div>"""


def _product(c: _Ctx) -> str:
    ui, lang = c.ui, c.lang
    products = c.products()
    name, category, price, desc, icon = products[0]
    stars = "".join(_ico("star", 18) for _ in range(5))
    checks = "".join(f'<li>{_ico("check", 18, "var(--color-primary)")}{esc(f.title[lang])}</li>'
                     for f in c.domain.features[:4])
    related = "".join(
        f'<a class="card mini" href="#"><div class="img">{_ico(p[4], 40)}</div><b>{esc(p[0])}</b>'
        f'<div class="muted">{_money(p[2], lang)}</div></a>'
        for p in products[1:5]
    )
    return f"""<header class="top"><div class="wrap">{c.brand()}<span style="display:flex;gap:1rem">
{_ico("search")}{_ico("heart")}{_ico("cart")}</span></div></header>
<div class="wrap"><div class="crumbs muted">{esc(ui["nav"][0])} / {esc(category)} / <b>{esc(name)}</b></div>
<div class="product"><div class="gallery"><div class="shot">{_ico(icon, 140, "#ffffff")}</div>
<div class="thumbs"><div></div><div></div><div></div><div></div></div></div>
<div><span class="badge">{esc(category)}</span><h1>{esc(name)}</h1>
<div class="stars">{stars}<span class="muted">&nbsp;4.9 · {c.rng.randint(40, 380)} {esc(ui["reviews"])}</span></div>
<p class="price">{_money(price, lang)}</p><p>{esc(desc)}</p>
<div class="buy"><div class="qty"><span>−</span><span>1</span><span>+</span></div>
<a class="btn btn-primary" href="#">{_ico("cart", 18)}{esc(ui["add_cart"])}</a></div>
<p class="muted" style="display:flex;gap:.5rem;align-items:center">{_ico("truck", 18)} {esc(ui["in_stock"])}</p><ul class="checks">{checks}</ul></div></div>
<div class="related"><h2>{esc(ui["related"])}</h2><div class="grid">{related}</div></div></div>"""


def _settings(c: _Ctx) -> str:
    ui = c.ui
    person = _PEOPLE[c.lang][0]
    tabs = "".join(f'<a href="#" class="{"active" if i == 0 else ""}">{esc(x)}</a>' for i, x in enumerate(ui["tabs"]))
    toggles = "".join(f'<div class="switch-row"><span>{esc(x)}</span><span class="toggle{" on" if i != 1 else ""}">'
                      "</span></div>" for i, x in enumerate(ui["notify"]))
    return f"""<div class="page"><div class="head"><span class="avatar">{esc(person[:1])}</span>
<div><h1 style="margin:0">{esc(person)}</h1><span class="muted">{esc(ui["member_since"])}</span></div></div>
<nav class="tabs">{tabs}</nav>
<div class="card section"><h3>{esc(ui["tabs"][0])}</h3><div class="fields">
<label>{esc(ui["name"])}<input value="{esc(person)}"></label>
<label>{esc(ui["email"])}<input type="email" value="noa@example.com"></label>
<label>{esc(ui["phone"])}<input type="tel" value="050-123-4567"></label>
<label>{esc(ui["language"])}<select><option>{esc(ui["lang_name"])}</option></select></label></div></div>
<div class="card section"><h3>{esc(ui["tabs"][1])}</h3>{toggles}</div>
<div class="card section"><div class="switch-row"><span>{_ico("shield", 18)} {esc(ui["two_factor"])}</span>
<span class="toggle on"></span></div></div>
<div class="actions"><a class="btn btn-ghost" href="#">{esc(ui["cancel"])}</a>
<a class="btn btn-primary" href="#">{esc(ui["save"])}</a></div></div>"""


def _mobile(c: _Ctx) -> str:
    ui, lang, copy = c.ui, c.lang, c.copy
    products = c.products()
    cats = []
    for p in products:
        if p[1] not in cats:
            cats.append(p[1])
    chips = "".join(f"<span>{esc(x)}</span>" for x in cats[:4])
    items = "".join(
        f'<div class="item"><div class="img">{_ico(p[4], 26)}</div><div class="grow"><b>{esc(p[0])}</b>'
        f'<small class="muted">{_money(p[2], lang)}</small></div><span class="plus">{_ico("plus", 16)}</span></div>'
        for p in products[:4]
    )
    tab_icons = ("home", "search", "cart", "user")
    tabbar = "".join(f'<a href="#" class="{"active" if i == 0 else ""}">{_ico(ic, 22)}{esc(label)}</a>'
                     for i, (ic, label) in enumerate(zip(tab_icons, ui["tabbar"], strict=False)))
    return f"""<div class="phone"><div class="screen"><div class="status"><span>9:41</span><span>●●● ▮</span></div>
<div class="content"><div class="hello"><span class="muted">{esc(ui["hello"])} 👋</span><h2>{esc(ui["today"])}</h2></div>
<div class="search">{_ico("search", 18)}{esc(ui["search"])}</div>
<div class="promo"><div><b>{esc(c.name)}</b><div style="opacity:.9;font-size:.9rem">{esc(copy["offer"])}</div></div>
{_ico(c.icons[0], 44, "#ffffff")}</div><div class="chips">{chips}</div>
<div class="row-head"><b>{esc(ui["popular"])}</b><a class="muted" href="#">{esc(ui["see_all"])}</a></div>
<div class="list">{items}</div></div><nav class="tabbar">{tabbar}</nav></div></div>"""


_BUILDERS = {"landing": _landing, "dashboard": _dashboard, "login": _login, "product": _product,
             "settings": _settings, "mobile": _mobile}


# ------------------------------------------------------------------------------------------ wireframes
def wireframe_svg(screen: str, lang: str = "he") -> str:
    """A grey, low-fidelity layout sketch of one screen (mirrored for right-to-left)."""
    rtl = lang == "he"
    if screen == "mobile":
        w, h = 200, 400
        boxes = [(14, 12, 172, 376, 26, "#f4f4f5"), (28, 40, 110, 12, 4, "#d4d4d8"), (28, 58, 140, 16, 4, "#a1a1aa"),
                 (28, 86, 144, 26, 10, "#e4e4e7"), (28, 122, 144, 60, 14, "#a1a1aa"), (28, 192, 40, 16, 8, "#d4d4d8"),
                 (74, 192, 40, 16, 8, "#e4e4e7"), (120, 192, 40, 16, 8, "#e4e4e7")]
        boxes += [(28, 218 + i * 38, 144, 30, 10, "#e4e4e7") for i in range(3)]
        boxes += [(14, 340, 172, 48, 0, "#d4d4d8")]
    else:
        w, h = 400, 260
        layouts: dict[str, list[tuple[float, float, float, float, float, str]]] = {
            "landing": [(0, 0, 400, 26, 0, "#d4d4d8"), (20, 46, 190, 18, 4, "#a1a1aa"), (20, 70, 160, 10, 3, "#d4d4d8"),
                        (20, 86, 140, 10, 3, "#d4d4d8"), (20, 106, 70, 18, 9, "#a1a1aa"), (250, 40, 130, 100, 14, "#e4e4e7")]
            + [(20 + i * 124, 160, 112, 70, 10, "#e4e4e7") for i in range(3)],
            "dashboard": [(0, 0, 80, 260, 0, "#d4d4d8"), (96, 12, 120, 14, 4, "#a1a1aa")]
            + [(96 + i * 76, 40, 68, 44, 8, "#e4e4e7") for i in range(4)]
            + [(96, 96, 190, 80, 8, "#e4e4e7"), (294, 96, 94, 80, 8, "#e4e4e7"), (96, 186, 292, 62, 8, "#e4e4e7")],
            "login": [(0, 0, 200, 260, 0, "#a1a1aa"), (240, 60, 120, 14, 4, "#a1a1aa"), (240, 92, 120, 20, 6, "#e4e4e7"),
                      (240, 122, 120, 20, 6, "#e4e4e7"), (240, 158, 120, 22, 8, "#71717a")],
            "product": [(0, 0, 400, 24, 0, "#d4d4d8"), (20, 40, 170, 130, 12, "#e4e4e7")]
            + [(20 + i * 44, 178, 38, 30, 6, "#f4f4f5") for i in range(4)]
            + [(210, 44, 150, 16, 4, "#a1a1aa"), (210, 68, 70, 14, 4, "#71717a"), (210, 92, 170, 34, 4, "#e4e4e7"),
               (210, 136, 90, 20, 10, "#71717a")],
            "settings": [(20, 14, 34, 34, 17, "#a1a1aa"), (62, 22, 100, 14, 4, "#d4d4d8"), (20, 60, 360, 14, 0, "#e4e4e7"),
                         (20, 84, 360, 80, 10, "#f4f4f5"), (20, 172, 360, 50, 10, "#f4f4f5"), (300, 230, 80, 20, 10, "#71717a")],
        }
        boxes = layouts.get(screen, layouts["landing"])
    shapes = []
    for x, y, bw, bh, r, fill in boxes:
        if rtl and screen != "mobile":
            x = w - x - bw
        shapes.append(f'<rect x="{x:g}" y="{y:g}" width="{bw:g}" height="{bh:g}" rx="{r:g}" fill="{fill}"/>')
    label = _LABELS.get(screen, (screen, screen))[0 if rtl else 1]
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img">'
            f'<title>{esc(label)}</title><rect width="{w}" height="{h}" fill="#fafafa"/>{"".join(shapes)}</svg>')


# ------------------------------------------------------------------------------------------ public API
def generate_mockup(brief: str, *, screens: Sequence[str] | None = None, palette: Palette | None = None,
                    lang: str | None = "he") -> list[Asset]:
    """HTML mockup screens, one SVG wireframe per screen and an index page linking everything."""
    lang = resolve_lang(lang, brief)
    palette = palette or generate_palette(brief)
    wanted = [s for s in (screens or SCREENS) if s in _BUILDERS] or list(SCREENS)
    ctx = _Ctx(brief, palette, lang)
    pages: list[Asset] = []
    frames: list[Asset] = []
    for screen in wanted:
        label = _LABELS[screen][0 if lang == "he" else 1]
        pages.append(Asset(name=f"mockup-{screen}", kind="html",
                           content=_page(f"{ctx.name} · {label}", _BUILDERS[screen](ctx), palette, lang, screen),
                           description=label))
        frames.append(Asset(name=f"wireframe-{screen}", kind="svg", content=wireframe_svg(screen, lang),
                            description=f"{ctx.ui['wireframe']}: {label}"))
    swatches = "".join(f'<span style="background:{palette.hex(role)}" title="{role}"></span>'
                       for role in ("primary", "secondary", "accent", "background", "text"))
    cards = "".join(
        f'<a class="card" href="{page.filename}"><div class="thumb">{frame.content}</div>'
        f'<div class="meta"><b>{esc(page.description)}</b><span class="badge">{esc(ctx.ui["open"])}</span></div></a>'
        for page, frame in zip(pages, frames, strict=True)
    )
    body = (f'<div class="page"><header>{ctx.brand()}<div><h1>{esc(ctx.ui["index_title"])} · {esc(ctx.name)}</h1>'
            f'<span class="muted">{esc(ctx.ui["index_sub"])}</span></div></header>'
            f'<div class="swatches" style="margin-bottom:1.5rem">{swatches}</div><div class="screens">{cards}</div></div>')
    index = Asset(name="index", kind="html", content=_page(f"{ctx.name} · {ctx.ui['index_title']}", body, palette,
                                                           lang, "index"),
                  description=ctx.ui["index_title"])
    return [index, *pages, *frames]
