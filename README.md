# JARVIS

JARVIS is a desktop AI assistant for Windows 10 and 11. One central orchestrator (JARVIS) splits work
between five specialist agents (Research, System, Development, Creative, Planner), takes commands by voice
or text in Hebrew and English, and asks before anything risky happens. The interface is a dark, neon-blue
command center.

<div dir="rtl">

## התחלה מהירה

**מה צריך:** Windows 10 או 11 (64 ביט), אינטרנט בזמן ההתקנה, ובערך 3GB פנויים בדיסק (כולל מודלי הדיבור).
שיחה חופשית, תכנון וחמשת הסוכנים עובדים בחינם על המחשב עצמו, עם Gemma 4 של Google בכרטיס הגרפי:
`jarvis chat install` (כ־7GB; כרטיס NVIDIA עם 12GB ומעלה). שום דבר לא יוצא מהמחשב. בלי מודל מקומי ובלי
מפתח, פקודות, פרויקטים ותוכניות עדיין עובדים. AI בתשלום (Grok, Claude, ChatGPT) **כבוי** כברירת מחדל: JARVIS
לא פונה אליו בכלל, גם כשיש מפתח שמור, וגם תמונות, סרטונים וזיהוי דיבור נעשים במחשב. אפשר להדליק אותו
ב**הגדרות ← ספקי AI ← AI בתשלום**; אז JARVIS משתמש בו רק כשאומרים לו: "תשתמש בגרוק" בתוך הבקשה (למשל
"תשתמש בגרוק ותכתוב לי שיר"), או "כן" כשהוא שואל "להשתמש ב-Grok?". אחרי זה Grok נשאר בשיחה עד שקט של שתי
דקות, ו"בלי גרוק" מפסיק אותו.

### התקנה מתיקיית הקוד

אם הקוד הגיע כקובץ zip שהורד מהאינטרנט, כדאי לפני החילוץ: קליק ימני על ה-zip ← מאפיינים ← לסמן
"בטל חסימה" (Unblock) ← אישור. כך Windows לא יעצור את הסקריפטים שבתוכו.

1. ללחוץ פעמיים על `setup.bat`.
   הסקריפט מחפש Python 3.11 עד 3.13 (64 ביט) ומציע להתקין Python 3.12 דרך winget אם אין; יוצר סביבה
   בתיקייה `.venv`; מתקין את JARVIS עם כל מה שהוא צריך (AI, קול, חלון, שילוב עם Windows); בודק את המחשב
   (`jarvis check`); ומציע קיצור דרך בשולחן העבודה. אפשר להריץ אותו שוב בכל זמן, הוא משלים רק את מה שחסר.
2. להפעיל: לחיצה כפולה על `run.bat` או על קיצור הדרך. מאותו רגע JARVIS עולה לבד ברקע בכל כניסה ל-Windows
   ומאזין ל-"Hey Jarvis" (כיבוי: **הגדרות ← כללי ← הפעלה עם Windows**).
3. מפתח AI בתשלום (רשות, ומשמש רק כש-AI בתשלום דלוק): במסך **הגדרות ← מפתחות API**, או בשורת הפקודה מתוך התיקייה:
   `jarvis keys set XAI_API_KEY` (Grok) או `jarvis keys set ANTHROPIC_API_KEY` (Claude). המפתח נשמר במנהל
   האישורים של Windows ואינו מוצג שוב, לא בממשק ולא ביומנים. באיזה ספק כל סוכן עובד בוחרים ב**הגדרות ← ספקי AI**.

### התקנה כתוכנה רגילה

אחרי `setup.bat -Dev`, מתוך התיקייה:

```
powershell -ExecutionPolicy Bypass -File scripts\build.ps1
```

הסקריפט מריץ את הבדיקות, בונה את `dist\JARVIS` (התוכנה `JARVIS.exe` וכלי שורת הפקודה `jarvis-cli.exe`),
בודק את מה שנבנה (`jarvis-cli check --full`) ויוצר את קובץ ההתקנה `dist\JARVIS-Setup-1.0.0.exe`.
לקובץ ההתקנה צריך Inno Setup 6.3 ומעלה: `winget install --id JRSoftware.InnoSetup --exact`.

ההתקנה היא למשתמש הנוכחי ואינה דורשת הרשאות מנהל. היא מוסיפה את JARVIS לתפריט התחל, ואפשר לבחור קיצור
דרך בשולחן העבודה. את ההפעלה עם הכניסה ל-Windows JARVIS מוסיף בעצמו בהפעלה הראשונה, וההסרה מוחקת אותה.
התוכנה אינה חתומה דיגיטלית, ולכן SmartScreen עשוי להזהיר בהפעלה הראשונה: "מידע נוסף" ואז "הפעל בכל זאת".

## איך מדברים עם JARVIS

- **בקול:** לומר "ג'רביס" (או "Hey Jarvis") ואז את הפקודה, בעברית או באנגלית, גם באותו משפט ("ג'רביס, מה השעה?"), או ללחוץ פעם אחת על Ctrl לבד ולדבר.
  JARVIS מאזין כל הזמן, גם כשהחלון סגור (נשאר רק העיגול הקטן בפינת המסך) וגם מיד אחרי הכניסה ל-Windows.
  אפשר גם להחזיק את כפתור המיקרופון במרכז הפיקוד. במחשב עם כרטיס מסך של NVIDIA זיהוי הדיבור רץ על הכרטיס,
  מהר ומדויק יותר (`setup.bat` מוסיף לשם כך את הקבצים של NVIDIA).
- **בכתב:** בשורת הפקודה במרכז הפיקוד.

| דוגמה | מה קורה |
|---|---|
| JARVIS, תבנה לי אפליקציה לניהול משימות | הסוכנים מתכננים, חוקרים, מעצבים, בונים ובודקים; הפרויקט נוצר ב-`%USERPROFILE%\JARVIS\projects` ונפתח לצפייה |
| תחקור את השוק של רכבים חשמליים | סוכן המחקר מחפש, קורא את המקורות ומחזיר סיכום עם המקורות |
| תפתח את כרום · תגביר את הווליום · מה מצב המחשב | סוכן המערכת מבצע מיד |
| מה מזג האוויר בחיפה מחר | תחזית |
| תכין לי תוכנית אימונים לשבוע | סוכן התכנון בונה תוכנית ושומר אותה כקובץ |
| תעצב לי לוגו לבית קפה · תכתוב מייל לדנה שאני חולה מחר | הסוכן היצירתי (מייל נשמר כטיוטה, לא נשלח) |
| תשלוט במחשב ותסדר לי את החלונות | סוכן המערכת מפעיל את העכבר והמקלדת בעצמו (ראו "שליטה במחשב" למטה) |
| הראה סוכנים פעילים · עצור את המשימה · תמשיך את המשימה | שליטה במשימות ובסוכנים |
| עצור הכול | עצירת חירום של כל הסוכנים והמשימות |
| תראה את עצמך · תסתיר את עצמך | מקפיץ את החלון הראשי, או מחזיר את JARVIS לרקע (הוא ממשיך להקשיב) |

בפס הצד יש שישה אזורים (Alt+1 עד Alt+6): מרכז פיקוד, קוד ופיתוח, יצירת תמונות, יצירת וידאו, אפליקציות וכלים,
והיסטוריה והגדרות (משימות, היסטוריית עבודה, סוכנים, זיכרון, אבטחה, הגדרות, פרטיות ונתונים).

### מצבי עבודה

| מצב | מה הוא עושה |
|---|---|
| CHAT | שיחה בלבד, בלי פעולות במחשב |
| NORMAL | ברירת המחדל: פעולות פשוטות מיד, משימות מורכבות מתחלקות בין הסוכנים |
| TASK | כל בקשה הופכת למשימה מתוכננת, והתוכנית מוצגת |
| AUTONOMOUS | שרשראות ארוכות והמשכים אוטומטיים; פעולות קריטיות עדיין מחכות לאישור |
| DEBUG | כמו NORMAL, עם פירוט הניתוב, התוכניות והכלים |

מעבר בין מצבים: "עבור למצב משימה", "switch to task mode", או בבורר המצבים בממשק.

## אזורי העבודה

בכל פעולה באזורים האלה מוצגים: מה JARVIS הבין שהתבקש, מה הוא עומד לעשות, אם זו פעולה אמיתית או **הדגמה**,
מה יוצא מהמחשב ולאן (ומחיר משוער כשזה עולה כסף), ההתקדמות שלב אחרי שלב, ובסוף התוצאה או שגיאה במילים.
לפעולה שרצה יש כפתור **עצירה**, ופעולה שנכשלה או נעצרה אפשר **לנסות שוב**. כל הפעולות נשמרות
ב**היסטוריה והגדרות ← היסטוריית עבודה**.

### קוד ופיתוח

1. **בחירת תיקיית הפרויקט.** JARVIS קורא את המבנה בלי AI: שפות, מסגרות, תלויות, ופקודות ההפעלה והבדיקה.
   הגישה היא לקריאה בלבד, ורק לתיקייה הזו.
2. **מה צריך?** כותבים בקשה חופשית ("להוסיף מתג למצב כהה"), או מדביקים שגיאה. JARVIS מסמן את הקבצים
   הרלוונטיים, ואפשר להוסיף או להוריד קבצים.
3. **לתכנן עם המודל המקומי** (או Grok / Claude, רק כש-AI בתשלום דלוק), במצב **מהיר**, **חזק** או **אוטומטי**. לפני
   השליחה מוצג בדיוק מה יישלח, לאן, ומחיר משוער; ערכים שנראים כמו סיסמאות או מפתחות מוסתרים לפני השליחה,
   ושום דבר לא נשלח בלי אישור.
4. מתקבלים **תוכנית קצרה, הנחות ושינויים** כ-diff לכל קובץ. אפשר לערוך קובץ ביד, להוציא קובץ מהשינוי,
   **לבקש תיקונים להצעה** במילים, או **לוותר**. שום דבר לא נכתב לדיסק לפני **להחיל את השינוי**, שדורש את
   המתג **לאפשר שינויים בפרויקט** ואישור. כל שינוי שהוחל אפשר **לבטל**, והקבצים חוזרים בדיוק למה שהיו.
5. **להריץ את הבדיקות**: הפקודה של הפרויקט עצמו, אחרי אישור. מוצג מה הורץ, כמה עברו, כמה נכשלו ואילו.
   כשמשהו נכשל, **לתקן עם...** שולח את הכישלונות ומחזיר הצעת תיקון לסקירה, כמו כל שינוי.

### יצירת תמונות

- היצירה היא **במחשב שלך ובחינם**, בכרטיס הגרפי: Z-Image-Turbo לתמונות ו-Wan 2.2 לסרטונים (מודלים פתוחים,
  Apache 2.0). שום דבר לא נשלח מהמחשב ושום דבר לא עולה כסף. מתקינים פעם אחת, הורדה של כ-70GB:
  `jarvis media install` (או `--dir <תיקייה>` לתיקייה שכבר יש בה אותם). `jarvis media status` מראה מה מותקן.
- תיאור בעברית מתורגם קודם לאנגלית, גם כן במחשב (opus-mt), כי המודלים מבינים אנגלית הכי טוב.
- לפני כל יצירה רואים את הטקסט שהמודל יקבל, ו"חינם · בערך X שניות". לפני הטעינה JARVIS בודק כמה זיכרון פנוי
  (RAM וכרטיס גרפי) ובוחר את הדרך המהירה שנכנסת; אם לא נכנס (למשל כשתוכנת AI גדולה אחרת פתוחה), הוא אומר כמה
  חסר ולא מתחיל, כדי לא לתקוע את המחשב. `jarvis media bench` מודד את המהירות במחשב.
- תיאור, סגנון, יחס תמונה ומספר וריאציות (עד 4), כמגה-פיקסל אחד. אפשר לשנות תמונה קיימת (קובץ שנבחר במפורש).
- בלי המודלים, המסך אומר מה חסר ומציע רק **הדגמה**: היא מסומנת בבירור ולא יוצרת תמונה אמיתית.
- תצוגה מקדימה לפני שמירה, וריאציות ותיקונים במילים ("שהשמיים יהיו כתומים"), ו**שמירה** למקום שבוחרים, עם
  שם קובץ והפורמט האמיתי של הקובץ. הדגמה אי אפשר לשמור.
- Grok Imagine של xAI (בתשלום) נשאר רק כבחירה מפורשת בהגדרות (`media.engine = "xai"`), ורק כש-AI בתשלום דלוק;
  כברירת מחדל הוא כבוי.

### יצירת וידאו

- תיאור סצנה, דמויות, תנועה, תאורה וסגנון; אורך 1 עד 5 שניות, יחס מסך ואיכות 480p או 720p. אפשר להתחיל
  מתמונה שנבחרה במפורש (תמונת פתיחה).
- גם כאן הכול במחשב ובחינם, והזמן המשוער מוצג מראש (סרטון לוקח כמה דקות). עצירה באמצע עוצרת את הכרטיס הגרפי
  מיד. בלי המודלים יש רק הדגמה מסומנת (לוח תכנון במקום סרטון).
- תצוגה מקדימה לפני שמירה, תיקונים במילים, ושמירה למקום שבוחרים (MP4).

### אפליקציות וכלים

- רשימת התוכנות המותקנות (מתפריט התחל) והחלונות הפתוחים, לקריאה בלבד. תוכנה נפתחת רק בלחיצה על "פתיחה" לידה.
- **להסתכל על המסך**: צילום מסך אחד בלחיצה, שמנותח במודל השליטה במחשב (Holo) במחשב הזה; שום דבר לא נשלח.
  JARVIS אומר באיזו אפליקציה ובאיזה מסך מדובר ומה הוא רואה, ואם הוא טעה אפשר לכתוב לו ("זה חלון השמירה").
- **משימה** ("לפתוח את המסמך", "למצוא את ההודעה"): JARVIS מציע צעד אחד הבא עם תצוגה מקדימה, ולא עושה כלום
  לפני **לבצע את הצעד** ואישור. שליחה, מחיקה, פרסום ותשלום מוצגים מראש ומאושרים שוב רגע לפני.
- עצירה מיד: כפתור העצירה, Esc פעמיים (גם כש-JARVIS מוסתר), או מתג החירום.
- JARVIS לא מקליד ולא לוחץ במסכי כניסה, בשדות סיסמה, במנהלי סיסמאות ובאבטחת Windows, ולא מגיע לחלונות של
  תוכנות שרצות כמנהל ולחלון ה-UAC. אין כרגע חיבור רשמי (API) לאף אפליקציה: העבודה היא דרך המסך בלבד.
- צריך Windows ואת מודל השליטה במחשב (`jarvis computer install`); בלעדיו המבט וההצעות לא זמינים, והמסך אומר למה.

### היסטוריה והגדרות

- **משימות** ו**היסטוריית עבודה**: כל משימה וכל פעולה של האזורים, עם התוצאה, ו"לנסות שוב".
- **מהירות** (בהיסטוריית העבודה): זמני תגובה שנמדדו בפועל; אפשר לשמור מצב "לפני" ולהשוות אליו.
- **פרטיות ונתונים**: מה JARVIS שומר, כמה ואיפה (בלי התוכן עצמו), ומחיקה לצמיתות של כל סוג בנפרד או של הכול,
  בלחיצה שנייה לאישור. עבודה שעדיין רצה נשארת. המסך גם מסביר מה מגיע למיקרופון, למסך ולאינטרנט: המיקרופון
  פתוח רק לזיהוי מילת ההפעלה במחשב (הצליל נזרק), והקלטת פקודה מתחילה רק אחרי מילת ההפעלה, Ctrl או כפתור
  המיקרופון; צילום מסך רק בלחיצה; AI בתשלום כבוי (וכשהוא דלוק, רק כשאומרים לו). מפתחות API, הגדרות, קבצי העבודה והמודלים לא נמחקים
  מכאן.

## שליטה במחשב (עכבר ומקלדת)

למשימות שאפשר לעשות רק דרך חלון של תוכנה, כשאין כלי ישיר שמתאים, JARVIS יכול להסתכל על המסך ולהפעיל את
העכבר והמקלדת בעצמו. מודל ראייה פתוח — **Holo3.1‑9B של H Company** (רישיון Apache‑2.0) — רץ מקומית על
כרטיס המסך דרך llama.cpp; הוא רק מציע את הפעולה הבאה, וצילומי המסך לא יוצאים מהמחשב.

- **מתקינים פעם אחת:** `jarvis computer install` מוריד את llama.cpp ואת המודל, מאמת כל קובץ מול ה‑SHA‑256
  של המפרסם, וממיר ל‑GGUF. תיקייה שכבר הורדה: `jarvis computer install --adopt <נתיב>`. מצב: `jarvis computer status`.
- **כל פעולה עוברת דרך מנגנון ההרשאות של JARVIS:** JARVIS בודק בעצמו מה נמצא מתחת לסמן או בחלון הפעיל (לא
  לפי מה שהמודל אומר), ומבקש אישור לפני לחיצה ששולחת הודעה, הרצת פקודה, לחצן תשלום וכדומה. שדות סיסמה, חלונות
  מערכת ו‑Ctrl+Alt+Del חסומים לחלוטין.
- **עצירה מיד:** Esc פעמיים (או "עצור הכול"). נגיעה בעכבר או במקלדת לא עוצרת את JARVIS, כך שאפשר להמשיך לעבוד
  לידו; אם עוברים לחלון אחר באמצע הקלדה, הוא מפסיק להקליד ולא שולח לשם אף מקש. מי שרוצה שגם נגיעה תעצור:
  ב‑`config.toml`, תחת `[computer]`, `stop_on_user_input = true`. בזמן העבודה JARVIS מסתיר את החלונות שלו כדי
  לא להפריע למסך, ומראה אותם רק כדי להציג אישור.
- צריך כרטיס מסך עם זיכרון סביר (נבדק על RTX 5060 Ti‏ 16GB; המודל תופס כ‑12GB).

## בטיחות

- פעולה מסוכנת או בלתי הפיכה (מחיקה רחבה, שינוי הגדרות מערכת, התקנת תוכנה, שליחת מידע החוצה) מחכה
  לאישור בחלון אישור שמסביר מה עומד לקרות. אין תשובה עד תום הזמן: הפעולה לא מתבצעת.
- הסוכנים כותבים רק בתיקיות העבודה (`%USERPROFILE%\JARVIS`) ובמה שאושר במפורש. תיקיות מערכת ומאגרי סיסמאות
  חסומים תמיד.
- **עצירת הכול:** הכפתור בממשק או "עצור הכול" בקול. עוצר את כל הסוכנים והמשימות וחוסם את הכלים עד לשחרור.
  לכל סוכן יש גם עצירת חירום משלו.
- מפתחות API אינם מוצגים בממשק, ביומנים או בקוד, ואינם מועברים לתוכנות ש-JARVIS מריץ.

## בדיקה שהכול עובד

| פקודה | מה נבדק |
|---|---|
| `jarvis check` | בדיקה מהירה: Python, תיקיות, זיכרון, שרת מקומי, ממשק וחלון, שליטה במחשב, מפתחות, AI, כלים, אינטרנט, קול |
| `jarvis check --full` | מפעיל JARVIS זמני ומריץ דרכו פקודות אמיתיות |
| `jarvis check --voice` או `jarvis voice-test` | מיקרופון, רמקולים, מילת ההפעלה וזיהוי דיבור (בפעם הראשונה מוריד את מודלי הדיבור) |

בתוכנה המותקנת יש בתפריט התחל קיצור "בדיקת מערכת של JARVIS".

## איפה JARVIS שומר דברים

| מה | איפה |
|---|---|
| הגדרות | `%LOCALAPPDATA%\JARVIS\config.toml` |
| זיכרון, היסטוריה ומשימות | `%LOCALAPPDATA%\JARVIS\jarvis.db` |
| יומנים | `%LOCALAPPDATA%\JARVIS\Logs` |
| מודלי דיבור | `%LOCALAPPDATA%\JARVIS\Cache\models` |
| קבצי עבודה ופרויקטים | `%USERPROFILE%\JARVIS\workspace`, `%USERPROFILE%\JARVIS\projects` |
| תצוגות מקדימות של תמונות וסרטונים | `%USERPROFILE%\JARVIS\workspace\media` |
| היסטוריית העבודה, שינויי קוד ומדידות מהירות | `%LOCALAPPDATA%\JARVIS` (`jobs.jsonl`, `dev\changes`, `metrics.json`) |
| מפתחות API | מנהל האישורים של Windows |

`jarvis paths` מציג את הנתיבים המדויקים במחשב. את מה שנשמר אפשר למחוק ב**היסטוריה והגדרות ← פרטיות ונתונים**.

## הסרה

- **תוכנה מותקנת:** הגדרות Windows ← אפליקציות ← JARVIS ← הסרה. המסיר שואל אם למחוק גם את הזיכרון,
  ההגדרות, היומנים, מודלי הדיבור ומפתחות ה-API. הפרויקטים ותיקיית העבודה נשארים בכל מקרה.
- **מתיקיית הקוד:** `jarvis stop`, ואז למחוק את התיקייה. כדי למחוק גם את הנתונים: `jarvis keys delete --all`
  ומחיקת `%LOCALAPPDATA%\JARVIS`.

## כשמשהו לא עובד

- **החלון לא נפתח:** בלי רכיב WebView2, JARVIS נפתח כחלון של Edge. `jarvis check` מראה מה חסר.
- **JARVIS לא שומע:** `jarvis voice-test`, ואז לבחור מיקרופון במסך **הגדרות ← קול**. כשלא נבחר מיקרופון,
  JARVIS עובר לבד למיקרופון ש-Windows מגדיר כברירת מחדל (למשל אוזניות שחוברו), בין שיחה לשיחה.
- **JARVIS לא עולה עם Windows:** `jarvis autostart` מראה אם הוא רשום, ואם כובה במנהל המשימות ← אפליקציות
  אתחול.
- **כל השאר:** היומן המלא נמצא ב-`%LOCALAPPDATA%\JARVIS\Logs\jarvis.log`.

</div>

## Reference

### Commands

Run them in the JARVIS folder (`jarvis.bat` calls the one in `.venv`; in PowerShell type `.\jarvis`).
The installed app has the same commands in `jarvis-cli.exe`.

| Command | What it does |
|---|---|
| `jarvis` | Start JARVIS with its window, or bring the running one forward |
| `jarvis --background` | Start with only the mini orb, listening for the wake word (how Windows starts it at sign-in) |
| `jarvis --no-window` | Run the core and the local server only; prints the address of the UI |
| `jarvis open` | Open the running JARVIS in the default browser |
| `jarvis say "<command>"` | Send a command to the running JARVIS and print the answer |
| `jarvis status` | Is JARVIS running? Agents, tasks, voice, AI provider |
| `jarvis stop` | Stop the running JARVIS (unfinished tasks are saved first) |
| `jarvis keys` | Which API keys are configured (never their values) |
| `jarvis keys set NAME` | Store an API key in Windows Credential Manager (typed hidden) |
| `jarvis keys delete NAME` | Remove a stored key (`--all`: every stored key) |
| `jarvis check [--full] [--voice] [--json]` | Is JARVIS ready on this PC? |
| `jarvis voice-test` | Microphone, speakers, wake word and speech recognition |
| `jarvis computer install [--dir D] [--adopt D] [--no-convert]` | Download/convert the local computer-control model (Holo3.1-9B), or adopt a folder already downloaded |
| `jarvis computer status` | Is the computer-control model installed, and where |
| `jarvis chat install [--dir D]` | Download the free local chat model (Gemma 4 12B, about 7 GB) JARVIS talks, plans and works with |
| `jarvis chat status` | Is the chat model installed, and where |
| `jarvis paths` | Where JARVIS keeps its data, logs, workspace and projects |
| `jarvis autostart` | Does JARVIS start with Windows? The setting and the sign-in entry |

Options: `--no-voice`, `--port N` (default 8765, the next free port if busy), `--debug`, `--quiet`,
`--browser` (the UI in the default browser instead of a window).

### Configuration

- Settings are edited in the app (Settings) and saved to `config.toml`.
- `general.start_with_windows` (on by default): the JARVIS window keeps a sign-in entry under
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` that starts `--background`; no administrator rights.
  Turning JARVIS off in Task Manager's Startup apps is respected.
- Speech recognition: faster-whisper `voice.stt_model` ("small", int8) on the CPU; with an NVIDIA graphics card
  and the `gpu` extra (NVIDIA's CUDA 12 libraries, which `setup.ps1` installs when it finds such a card)
  `voice.stt_model_gpu` ("large-v3-turbo", int8_float16) takes over in the background once it is loaded on the
  card, and the CPU model comes back if the card fails. `voice.ctrl_to_talk`: a lone Ctrl tap starts listening.
- Any setting can be overridden with an environment variable `JARVIS__<SECTION>__<KEY>`, for example
  `JARVIS__VOICE__ENABLED=false` or `JARVIS__AI__PROVIDER=openai`.
- `JARVIS_HOME=<folder>` keeps everything (data, logs, cache, workspace) under one folder: portable mode.
- The local model (`ai.local`): JARVIS runs its own llama.cpp server with Gemma 4 12B (QAT, 4-bit) on
  127.0.0.1 with a random key, started at the first request and stopped after `idle_minutes` unused or when
  computer control or image/video creation needs the graphics card; `base_url` points it at another
  OpenAI-compatible server (Ollama, LM Studio) instead. It is free, so it answers without asking.
- AI providers: Anthropic Claude (default model `claude-opus-5-5`, with Anthropic's server-side fallbacks on),
  xAI Grok (`grok-4.7`), OpenAI, or the local model. The five agents ask the
  provider set for them in `ai.agent_providers` first (Grok by default) and fall back to JARVIS's order,
  Anthropic → xAI → OpenAI → local, which JARVIS's own calls use. With `ai.allow_paid` off (the default) none
  of Anthropic, xAI and OpenAI is ever called, whatever keys are saved: chat, the agents, planning, dev mode,
  images, videos and speech recognition all run on this computer, and naming a paid AI gets "paid AI is off".
  With it on and `ai.cloud_only_when_told` (on by default) a cloud provider is used only when the user names it in the request ("use Grok ...", "תשתמש בגרוק ...") or says
  yes when JARVIS asks; the choice lasts for that conversation and goes with the tasks it starts. A local model
  needs no word. API keys: `ANTHROPIC_API_KEY`, `XAI_API_KEY`,
  `OPENAI_API_KEY`; optional `BRAVE_API_KEY` or `TAVILY_API_KEY` (web search), `PICOVOICE_ACCESS_KEY`
  (Porcupine wake word), `GITHUB_TOKEN` (Development agent).

### Development

- `setup.bat -Dev` adds the developer tools (pytest, ruff, mypy, Playwright, PyInstaller).
- Tests: `.venv\Scripts\python -m pytest tests`; on Windows also `-m windows` for the desktop integration.
  [docs/TESTING.md](docs/TESTING.md) maps every required behaviour to its tests.
- Interface (`ui/`, React + TypeScript): `npm ci`, `npm test`, `npm run build` (writes
  `src/jarvis/ui/static`, which is committed). `npm run dev` serves it with hot reload; `/?mock=1` runs it
  against the built-in simulator.
- Build the app and the installer: `scripts\build.ps1` (`-SkipTests`, `-SkipInstaller`).
- Speed: `python scripts/bench_local.py` times everyday commands on this computer with no AI and no network
  (`--src <other>/src` for a before/after comparison).
- The icon is drawn by `python scripts/make_icon.py`.
- Computer control lives in `src/jarvis/computer/` (the model server, the look→think→act session, and the
  safety policy judged from what Windows reports); the agent-facing tools are `use_computer` / `computer_act`.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): components, agents, orchestration, security model.
  [docs/API.md](docs/API.md): the local HTTP and WebSocket API the interface uses.

```
src/jarvis/
  core/           intents (Hebrew and English), modes, command routing
  agents/         the five agents and their skills
  orchestration/  task plans, parallel execution, retries, results
  tools/          file, process, web, project and system tools, all behind the permission gate
  permissions/    capabilities, grants, approvals, kill switches
  security/       path and network guards, secret store, redaction, code checks
  memory/         short-term, long-term and project memory (SQLite)
  voice/          wake word, speech recognition, speech synthesis
  ai/             Anthropic, xAI, OpenAI and local providers; local_server/local_install: the own chat model
  devmode/        the code screen: project inspection, AI proposals, review, apply and undo, test runs
  media/          image and video creation on this computer (local/: the worker, its installer and bench), Grok
                  Imagine when chosen, and the marked demonstration
  integrations/   the apps screen: installed apps, open windows, one look at the screen, one step at a time
  jobs.py         the work screens' jobs (steps, progress, stop, retry, history)
  metrics.py      measured response times behind the speed report
  privacy.py      what JARVIS keeps, and deleting it for good
  server/         FastAPI on 127.0.0.1 with a per-run token
  ui/             the desktop window; the built interface
  host/           Windows (and POSIX) integration
ui/               the interface source
packaging/        PyInstaller spec and Inno Setup script
scripts/          setup, build, icon
tests/            unit, integration and end-to-end tests
```
