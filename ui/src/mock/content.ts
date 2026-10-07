// Bilingual content for the in-browser simulator. Nothing here is used by the live UI.
import type { AgentId, Lang, Priority, RiskLevel } from '../api/types';

export interface Bi {
  he: string;
  en: string;
}

export interface BiList {
  he: string[];
  en: string[];
}

export const L = (he: string, en: string): Bi => ({ he, en });
export const LL = (he: string[], en: string[]): BiList => ({ he, en });

export function pick(text: Bi, lang: Lang): string {
  return text[lang];
}

export interface AgentProfile {
  id: AgentId;
  number: 1 | 2 | 3 | 4 | 5;
  name: string;
  name_he: string;
  role: string;
  role_he: string;
  permissions: string[];
  skills: string[];
}

export const AGENT_PROFILES: AgentProfile[] = [
  {
    id: 'research',
    number: 1,
    name: 'Research Agent',
    name_he: 'סוכן מחקר',
    role: 'Web research, sources and summaries',
    role_he: 'מחקר ברשת, מקורות וסיכומים',
    permissions: ['net.search', 'net.fetch', 'net.api', 'fs.read', 'fs.write', 'memory.read', 'memory.write', 'ai.generate', 'system.info'],
    skills: ['web_search', 'fetch_page', 'summarize', 'compare', 'fact_check'],
  },
  {
    id: 'system',
    number: 2,
    name: 'System Agent',
    name_he: 'סוכן מערכת',
    role: 'Apps, files, processes and Windows settings',
    role_he: 'אפליקציות, קבצים, תהליכים והגדרות Windows',
    permissions: [
      'system.info',
      'process.list',
      'process.launch',
      'process.kill',
      'shell.execute',
      'system.settings',
      'system.power',
      'fs.read',
      'fs.write',
      'fs.delete',
      'notify',
      'clipboard',
      'memory.read',
      'memory.write',
      'ai.generate',
    ],
    skills: ['open_app', 'close_app', 'system_status', 'volume', 'screenshot', 'lock', 'organize_folder'],
  },
  {
    id: 'development',
    number: 3,
    name: 'Development Agent',
    name_he: 'סוכן פיתוח',
    role: 'Code, projects, tests and builds',
    role_he: 'קוד, פרויקטים, בדיקות ובנייה',
    permissions: [
      'fs.read',
      'fs.write',
      'fs.delete',
      'code.execute',
      'code.install',
      'shell.execute',
      'process.launch',
      'process.list',
      'git',
      'net.fetch',
      'net.search',
      'memory.read',
      'memory.write',
      'ai.generate',
      'system.info',
    ],
    skills: ['scaffold_project', 'write_code', 'run_tests', 'build', 'git_commit'],
  },
  {
    id: 'creative',
    number: 4,
    name: 'Creative Agent',
    name_he: 'סוכן יצירתי',
    role: 'Design, copy, images and presentations',
    role_he: 'עיצוב, תוכן, תמונות ומצגות',
    permissions: ['fs.read', 'fs.write', 'ai.generate', 'net.fetch', 'memory.read', 'memory.write', 'process.launch'],
    skills: ['write_copy', 'color_palette', 'create_slides', 'generate_image', 'brand_kit'],
  },
  {
    id: 'planner',
    number: 5,
    name: 'Planner Agent',
    name_he: 'סוכן תכנון',
    role: 'Plans, breaks down and schedules work',
    role_he: 'מתכנן, מפרק ומתזמן משימות',
    permissions: ['fs.read', 'fs.write', 'memory.read', 'memory.write', 'ai.generate', 'system.info'],
    skills: ['plan_task', 'estimate', 'schedule', 'review_plan'],
  },
];

export const CAPABILITIES = [
  'fs.read',
  'fs.write',
  'fs.delete',
  'process.list',
  'process.launch',
  'process.kill',
  'shell.execute',
  'system.info',
  'system.settings',
  'system.power',
  'net.search',
  'net.fetch',
  'net.api',
  'code.execute',
  'code.install',
  'git',
  'memory.read',
  'memory.write',
  'ai.generate',
  'notify',
  'clipboard',
];

export const RISK_CEILINGS: Record<AgentId, string> = {
  research: 'HIGH',
  system: 'CRITICAL',
  development: 'CRITICAL',
  creative: 'HIGH',
  planner: 'MEDIUM',
};

export interface ConfirmSpec {
  tool: string;
  capability: string;
  risk: RiskLevel;
  summary: Bi;
  reasons: BiList;
  details: Record<string, unknown>;
  denied: Bi;
}

export interface SubtaskTemplate {
  key: string;
  agent: AgentId;
  title: Bi;
  instruction: Bi;
  depends: string[];
  seconds: number;
  actions: BiList;
  summary: Bi;
  files?: string[];
  tool?: { name: string; capability: string; risk: RiskLevel };
  confirm?: ConfirmSpec;
  failOnce?: Bi;
  optional?: boolean;
}

export interface TaskTemplate {
  key: string;
  title: Bi;
  goal: Bi;
  strategy: Bi;
  priority: Priority;
  subtasks: SubtaskTemplate[];
  result: { summary: Bi; details: Bi | null; actions: BiList; issues: BiList; next: Bi };
}

const WS = 'C:\\Users\\Dana\\JARVIS\\workspace';
const PR = 'C:\\Users\\Dana\\JARVIS\\projects';

export const PATHS = { workspace: WS, projects: PR };

export const TASK_TEMPLATES: TaskTemplate[] = [
  {
    key: 'landing',
    title: L('דף נחיתה לבית קפה „אורה”', 'Landing page for “Ora” coffee shop'),
    goal: L(
      'לבנות דף נחיתה מודרני ומהיר לבית הקפה אורה, עם תפריט, שעות פתיחה וכפתור הזמנה, ולפתוח אותו בדפדפן.',
      'Build a fast, modern landing page for Ora coffee shop with a menu, opening hours and an order button, then open it in the browser.',
    ),
    strategy: L(
      'תכנון מבנה → מחקר ותוכן במקביל → פיתוח → בדיקה ופתיחה',
      'Plan the structure → research and copy in parallel → build → verify and open',
    ),
    priority: 'high',
    subtasks: [
      {
        key: 'plan',
        agent: 'planner',
        title: L('תכנון מבנה הדף', 'Plan the page structure'),
        instruction: L('פרק את הדף לאזורים והגדר קריטריוני קבלה.', 'Break the page into sections and define acceptance criteria.'),
        depends: [],
        seconds: 7,
        actions: LL(['ניתוח הבקשה', 'הגדרת 5 אזורים בדף', 'כתיבת קריטריוני קבלה'], ['Analyzed the request', 'Defined 5 page sections', 'Wrote acceptance criteria']),
        summary: L('מבנה של 5 אזורים: פתיח, תפריט, אודות, שעות ומיקום, הזמנה.', '5 sections: hero, menu, about, hours & location, order.'),
        files: [`${PR}\\ora-coffee\\PLAN.md`],
        tool: { name: 'fs.write_file', capability: 'fs.write', risk: 'low' },
      },
      {
        key: 'research',
        agent: 'research',
        title: L('מחקר דפי נחיתה של בתי קפה', 'Research coffee shop landing pages'),
        instruction: L('מצא 5 דוגמאות מובילות וסכם דפוסים טובים.', 'Find 5 strong examples and summarize what works.'),
        depends: ['plan'],
        seconds: 13,
        actions: LL(
          ['חיפוש: coffee shop landing page', 'נקראו 6 מקורות', 'סיכום דפוסים: תמונות גדולות, תפריט קצר, CTA בולט'],
          ['Searched: coffee shop landing page', 'Read 6 sources', 'Summarized patterns: big imagery, short menu, clear CTA'],
        ),
        summary: L('נמצאו 6 מקורות; המלצה: תמונה גדולה, תפריט קצר וכפתור הזמנה קבוע.', '6 sources; recommend a hero image, a short menu and a sticky order button.'),
        files: [`${PR}\\ora-coffee\\research.md`],
        tool: { name: 'web.search', capability: 'net.search', risk: 'low' },
      },
      {
        key: 'copy',
        agent: 'creative',
        title: L('כתיבת תוכן ובחירת פלטת צבעים', 'Write copy and pick a color palette'),
        instruction: L('כתוב כותרות ותיאורים חמים ובחר פלטה בגווני קפה.', 'Write warm headlines and copy, and choose a coffee-toned palette.'),
        depends: ['plan'],
        seconds: 11,
        actions: LL(['נכתבו 4 כותרות', 'נבחרה פלטה: #3B2A20 #C8A27A #F4EDE4', 'נוצר תיאור לתפריט'], ['Wrote 4 headlines', 'Picked palette #3B2A20 #C8A27A #F4EDE4', 'Wrote menu descriptions']),
        summary: L('תוכן מלא בעברית ובאנגלית ופלטה של 3 צבעים.', 'Full Hebrew/English copy and a 3-color palette.'),
        files: [`${PR}\\ora-coffee\\copy.json`],
        tool: { name: 'ai.generate', capability: 'ai.generate', risk: 'low' },
      },
      {
        key: 'build',
        agent: 'development',
        title: L('פיתוח הדף ב-HTML/CSS', 'Build the HTML/CSS page'),
        instruction: L('בנה דף רספונסיבי לפי התוכנית, התוכן והפלטה.', 'Build a responsive page from the plan, copy and palette.'),
        depends: ['research', 'copy'],
        seconds: 18,
        actions: LL(['נוצר שלד הפרויקט', 'נכתב index.html', 'נכתב styles.css', 'הורצה בדיקת קישורים'], ['Scaffolded the project', 'Wrote index.html', 'Wrote styles.css', 'Checked links']),
        summary: L('דף רספונסיבי עם 5 אזורים; כל הקישורים תקינים.', 'Responsive page with 5 sections; all links resolve.'),
        files: [`${PR}\\ora-coffee\\index.html`, `${PR}\\ora-coffee\\styles.css`],
        tool: { name: 'code.write_files', capability: 'fs.write', risk: 'low' },
        confirm: {
          tool: 'shell.run',
          capability: 'code.install',
          risk: 'high',
          summary: L('התקנת חבילות npm בפרויקט ora-coffee (3 חבילות)', 'Install 3 npm packages in the ora-coffee project'),
          reasons: LL(['התקנת תוכנה מהרשת', 'הרצת סקריפטים של התקנה'], ['Installs software from the internet', 'Runs package install scripts']),
          details: { command: 'npm install vite sharp lightningcss', cwd: `${PR}\\ora-coffee`, packages: 3 },
          denied: L('ההתקנה נדחתה — נבנה דף סטטי ללא חבילות.', 'Install denied — built a static page without packages.'),
        },
      },
      {
        key: 'open',
        agent: 'system',
        title: L('בדיקה ופתיחה בדפדפן', 'Verify and open in the browser'),
        instruction: L('ודא שהקבצים קיימים ופתח את הדף.', 'Check the files exist and open the page.'),
        depends: ['build'],
        seconds: 5,
        actions: LL(['אומתו 2 קבצים', 'נפתח index.html ב-Chrome'], ['Verified 2 files', 'Opened index.html in Chrome']),
        summary: L('הדף נפתח ב-Chrome.', 'The page is open in Chrome.'),
        tool: { name: 'process.open', capability: 'process.launch', risk: 'low' },
      },
    ],
    result: {
      summary: L(
        'דף הנחיתה של בית הקפה אורה מוכן ונפתח בדפדפן. הוא כולל פתיח, תפריט, אודות, שעות ומיקום וכפתור הזמנה.',
        'The Ora coffee shop landing page is ready and open in the browser, with a hero, menu, about, hours & location and an order button.',
      ),
      details: L(
        '- 5 אזורים לפי התוכנית\n- תוכן בעברית ובאנגלית\n- עיצוב רספונסיבי ונגיש\n- נבדק: כל הקישורים תקינים',
        '- 5 sections as planned\n- Hebrew and English copy\n- Responsive, accessible layout\n- Verified: all links resolve',
      ),
      actions: LL(['נוצרה תיקיית פרויקט', 'נכתבו 5 קבצים', 'הדף נפתח ב-Chrome'], ['Created a project folder', 'Wrote 5 files', 'Opened the page in Chrome']),
      issues: LL([], []),
      next: L('להוסיף תמונות אמיתיות של בית הקפה ולפרסם.', 'Add real photos of the shop and publish it.'),
    },
  },
  {
    key: 'headphones',
    title: L('השוואת אוזניות מבטלות רעשים', 'Compare noise-cancelling headphones'),
    goal: L('למצוא את 3 האוזניות הטובות עד ₪1,500 ולהכין טבלת השוואה.', 'Find the 3 best headphones under ₪1,500 and make a comparison table.'),
    strategy: L('תכנון → שני מחקרים במקביל → טבלה מעוצבת', 'Plan → two research tracks in parallel → designed table'),
    priority: 'normal',
    subtasks: [
      {
        key: 'plan',
        agent: 'planner',
        title: L('הגדרת קריטריונים', 'Define comparison criteria'),
        instruction: L('בחר קריטריונים: ביטול רעשים, נוחות, סוללה, מחיר.', 'Pick criteria: ANC, comfort, battery, price.'),
        depends: [],
        seconds: 5,
        actions: LL(['נקבעו 6 קריטריונים'], ['Set 6 criteria']),
        summary: L('6 קריטריונים עם משקלות.', '6 weighted criteria.'),
      },
      {
        key: 'reviews',
        agent: 'research',
        title: L('איסוף ביקורות', 'Collect expert reviews'),
        instruction: L('אסוף ביקורות מקצועיות על 8 דגמים.', 'Collect expert reviews for 8 models.'),
        depends: ['plan'],
        seconds: 12,
        actions: LL(['נקראו 9 ביקורות', 'דורגו 8 דגמים'], ['Read 9 reviews', 'Scored 8 models']),
        summary: L('3 דגמים מובילים בציון משוקלל.', '3 models lead on the weighted score.'),
        tool: { name: 'web.fetch', capability: 'net.fetch', risk: 'low' },
      },
      {
        key: 'prices',
        agent: 'research',
        title: L('השוואת מחירים בישראל', 'Compare prices in Israel'),
        instruction: L('מצא מחירים עדכניים ב-4 חנויות.', 'Find current prices in 4 stores.'),
        depends: ['plan'],
        seconds: 9,
        actions: LL(['נבדקו 4 חנויות', 'נמצא מחיר הטוב ביותר לכל דגם'], ['Checked 4 stores', 'Found the best price per model']),
        summary: L('מחירים מ-₪890 עד ₪1,450.', 'Prices from ₪890 to ₪1,450.'),
        tool: { name: 'web.search', capability: 'net.search', risk: 'low' },
      },
      {
        key: 'table',
        agent: 'creative',
        title: L('עיצוב טבלת השוואה', 'Design the comparison sheet'),
        instruction: L('הכן טבלה ברורה עם המלצה.', 'Make a clear table with a recommendation.'),
        depends: ['reviews', 'prices'],
        seconds: 8,
        actions: LL(['נוצרה טבלה', 'נוספה המלצה'], ['Created the table', 'Added a recommendation']),
        summary: L('טבלת השוואה עם המלצה ברורה.', 'Comparison table with a clear pick.'),
        files: [`${WS}\\headphones-comparison.html`],
        tool: { name: 'fs.write_file', capability: 'fs.write', risk: 'low' },
      },
    ],
    result: {
      summary: L('ההמלצה: Sony WH-1000XM5 — הכי טובה בביטול רעשים, ב-₪1,290. הטבלה המלאה שמורה בסביבת העבודה.', 'Pick: Sony WH-1000XM5 — best noise cancelling at ₪1,290. The full table is in the workspace.'),
      details: L('- Sony WH-1000XM5 — ₪1,290\n- Bose QC Ultra — ₪1,450\n- Soundcore Space One — ₪890', '- Sony WH-1000XM5 — ₪1,290\n- Bose QC Ultra — ₪1,450\n- Soundcore Space One — ₪890'),
      actions: LL(['נקראו 9 ביקורות', 'נבדקו 4 חנויות', 'נוצרה טבלת השוואה'], ['Read 9 reviews', 'Checked 4 stores', 'Created a comparison table']),
      issues: LL(['חנות אחת לא הציגה מלאי עדכני'], ['One store had no current stock data']),
      next: L('לבדוק מבצעים לפני הרכישה.', 'Check for deals before buying.'),
    },
  },
  {
    key: 'organizer',
    title: L('סקריפט לסידור תיקיית ההורדות', 'Script that organizes the Downloads folder'),
    goal: L('לכתוב סקריפט Python שממיין את ההורדות לתיקיות לפי סוג, ולהריץ אותו.', 'Write a Python script that sorts Downloads into folders by type, and run it.'),
    strategy: L('תכנון → כתיבה ובדיקות → הרצה מבוקרת', 'Plan → write and test → controlled run'),
    priority: 'normal',
    subtasks: [
      {
        key: 'plan',
        agent: 'planner',
        title: L('תכנון כללי המיון', 'Plan the sorting rules'),
        instruction: L('הגדר קטגוריות לפי סיומות.', 'Define categories by extension.'),
        depends: [],
        seconds: 5,
        actions: LL(['הוגדרו 7 קטגוריות'], ['Defined 7 categories']),
        summary: L('7 קטגוריות: מסמכים, תמונות, וידאו, מוזיקה, ארכיונים, התקנות, אחר.', '7 categories: documents, images, video, music, archives, installers, other.'),
      },
      {
        key: 'code',
        agent: 'development',
        title: L('כתיבת הסקריפט', 'Write the script'),
        instruction: L('כתוב organize_downloads.py עם מצב dry-run.', 'Write organize_downloads.py with a dry-run mode.'),
        depends: ['plan'],
        seconds: 12,
        actions: LL(['נכתב organize_downloads.py', 'נוספו בדיקות יחידה'], ['Wrote organize_downloads.py', 'Added unit tests']),
        summary: L('סקריפט עם dry-run ו-6 בדיקות.', 'Script with dry-run and 6 tests.'),
        files: [`${PR}\\downloads-organizer\\organize_downloads.py`, `${PR}\\downloads-organizer\\test_organize.py`],
        failOnce: L('SyntaxError בשורה 42 — מתקן ומנסה שוב', 'SyntaxError on line 42 — fixing and retrying'),
        tool: { name: 'code.write_files', capability: 'fs.write', risk: 'low' },
      },
      {
        key: 'run',
        agent: 'system',
        title: L('הרצה על תיקיית ההורדות', 'Run it on Downloads'),
        instruction: L('הרץ קודם dry-run ואז הזז קבצים.', 'Dry-run first, then move files.'),
        depends: ['code'],
        seconds: 8,
        actions: LL(['dry-run: 214 קבצים', 'הועברו 214 קבצים ל-7 תיקיות'], ['Dry run: 214 files', 'Moved 214 files into 7 folders']),
        summary: L('214 קבצים מוינו ל-7 תיקיות.', 'Sorted 214 files into 7 folders.'),
        tool: { name: 'fs.move', capability: 'fs.write', risk: 'medium' },
      },
    ],
    result: {
      summary: L('תיקיית ההורדות מסודרת: 214 קבצים מוינו ל-7 תיקיות. הסקריפט שמור בפרויקטים.', 'Downloads is organized: 214 files sorted into 7 folders. The script is saved in Projects.'),
      details: L('אפשר להריץ שוב בכל עת: `python organize_downloads.py --dry-run`', 'Run it again any time: `python organize_downloads.py --dry-run`'),
      actions: LL(['נכתב סקריפט ובדיקות', 'הועברו 214 קבצים'], ['Wrote a script and tests', 'Moved 214 files']),
      issues: LL(['ניסיון ראשון נכשל ב-SyntaxError ותוקן'], ['First attempt failed with a SyntaxError and was fixed']),
      next: L('לתזמן הרצה שבועית.', 'Schedule a weekly run.'),
    },
  },
];

export const GENERIC_TEMPLATE = (title: string, creative: boolean): TaskTemplate => ({
  key: 'generic',
  title: L(title, title),
  goal: L(title, title),
  strategy: L('תכנון → מחקר → ביצוע → אימות', 'Plan → research → execute → verify'),
  priority: 'normal',
  subtasks: [
    {
      key: 'plan',
      agent: 'planner',
      title: L('פירוק הבקשה לשלבים', 'Break the request into steps'),
      instruction: L(title, title),
      depends: [],
      seconds: 5,
      actions: LL(['נבנתה תוכנית של 4 שלבים'], ['Built a 4-step plan']),
      summary: L('תוכנית של 4 שלבים.', 'A 4-step plan.'),
    },
    {
      key: 'research',
      agent: 'research',
      title: L('איסוף מידע רלוונטי', 'Gather relevant information'),
      instruction: L(title, title),
      depends: ['plan'],
      seconds: 9,
      actions: LL(['נקראו 5 מקורות'], ['Read 5 sources']),
      summary: L('סיכום של 5 מקורות.', 'Summary of 5 sources.'),
      tool: { name: 'web.search', capability: 'net.search', risk: 'low' },
    },
    {
      key: 'make',
      agent: creative ? 'creative' : 'development',
      title: creative ? L('יצירת התוצר', 'Create the deliverable') : L('בניית הפתרון', 'Build the solution'),
      instruction: L(title, title),
      depends: ['research'],
      seconds: 12,
      actions: LL(['נוצר תוצר ראשון', 'בוצעו שיפורים'], ['Created a first version', 'Refined it']),
      summary: L('התוצר מוכן.', 'The deliverable is ready.'),
      files: [`${WS}\\${creative ? 'deliverable.pptx' : 'solution\\main.py'}`],
      tool: { name: 'fs.write_file', capability: 'fs.write', risk: 'low' },
    },
    {
      key: 'verify',
      agent: 'system',
      title: L('אימות ופתיחה', 'Verify and open'),
      instruction: L(title, title),
      depends: ['make'],
      seconds: 4,
      actions: LL(['אומתו הקבצים'], ['Verified the files']),
      summary: L('הכול תקין.', 'All good.'),
    },
  ],
  result: {
    summary: L(`סיימתי: ${title}. התוצרים שמורים בסביבת העבודה.`, `Done: ${title}. The results are in the workspace.`),
    details: null,
    actions: LL(['בוצעו 4 שלבים'], ['Completed 4 steps']),
    issues: LL([], []),
    next: L('לעבור על התוצר ולתת משוב.', 'Review the result and give feedback.'),
  },
});

export interface VoiceScript {
  transcript: Bi;
  intent: string;
  action: { title: Bi; icon: string } | null;
  reply: Bi;
  fails?: { title: Bi; detail: Bi };
}

export const VOICE_SCRIPTS: VoiceScript[] = [
  {
    transcript: L('פתח לי את כרום', 'Open Chrome for me'),
    intent: 'open_app',
    action: { title: L('פותח את Google Chrome…', 'Opening Google Chrome…'), icon: 'chrome' },
    reply: L('נפתח.', 'Done.'),
  },
  {
    transcript: L('מה מצב המערכת?', 'How is the system doing?'),
    intent: 'system_status',
    action: null,
    reply: L('המעבד ב-{cpu} אחוז והזיכרון ב-{mem} אחוז. הכול תקין.', 'CPU is at {cpu} percent and memory at {mem} percent. All good.'),
  },
  {
    transcript: L('פתח את יוטיוב', 'Open YouTube'),
    intent: 'open_website',
    action: { title: L('פותח את YouTube…', 'Opening YouTube…'), icon: 'youtube' },
    reply: L('פתחתי את יוטיוב.', 'YouTube is open.'),
  },
  {
    transcript: L('פתח לי את כרום', 'Open Chrome for me'),
    intent: 'open_app',
    action: { title: L('פותח את Google Chrome…', 'Opening Google Chrome…'), icon: 'chrome' },
    reply: L('לא הצלחתי לפתוח את כרום.', 'I couldn’t open Chrome.'),
    fails: {
      title: L('לא הצלחתי לפתוח את כרום.', 'I couldn’t open Chrome.'),
      detail: L('נסה שוב או בדוק את החיבור.', 'Try again or check the connection.'),
    },
  },
  {
    transcript: L('תעלה את הווליום ל-40', 'Set the volume to 40'),
    intent: 'volume',
    action: { title: L('מכוון עוצמת שמע ל-40%…', 'Setting volume to 40%…'), icon: 'volume' },
    reply: L('העוצמה על 40.', 'Volume is at 40.'),
  },
];

export const NOTIFICATIONS: { level: string; title: Bi; message: Bi }[] = [
  { level: 'info', title: L('יש לך הודעה חדשה', 'You have a new message'), message: L('במייל מהלקוח', 'Email from the client') },
  { level: 'success', title: L('ההורדה הושלמה', 'Download complete'), message: L('report-q3.pdf נשמר בהורדות', 'report-q3.pdf saved to Downloads') },
  { level: 'info', title: L('תזכורת מהיומן', 'Calendar reminder'), message: L('פגישת צוות בעוד 15 דקות', 'Team sync in 15 minutes') },
  { level: 'warning', title: L('הסוללה ב-20%', 'Battery at 20%'), message: L('מומלץ לחבר את המטען', 'Consider plugging in the charger') },
  { level: 'info', title: L('עדכון Windows מוכן', 'Windows update ready'), message: L('יותקן בהפעלה הבאה', 'It will install on next restart') },
];

export const APP_NAMES: { match: RegExp; name: string; icon: string }[] = [
  { match: /(כרום|chrome)/i, name: 'Google Chrome', icon: 'chrome' },
  { match: /(יוטיוב|youtube)/i, name: 'YouTube', icon: 'youtube' },
  { match: /(מחשבון|calc)/i, name: 'Calculator', icon: 'calculator' },
  { match: /(קבצים|סייר|explorer|files)/i, name: 'File Explorer', icon: 'folder' },
  { match: /(הגדרות|settings)/i, name: 'Settings', icon: 'settings' },
  { match: /(ספוטיפיי|spotify)/i, name: 'Spotify', icon: 'music' },
  { match: /(vs ?code|visual studio|קוד)/i, name: 'Visual Studio Code', icon: 'code' },
  { match: /(וורד|word)/i, name: 'Microsoft Word', icon: 'file' },
];

export const GENERIC_REPLIES: BiList = LL(
  [
    'אני כאן. אפשר לבקש ממני לפתוח אפליקציות, לחקור נושאים, לבנות פרויקטים ולנהל את המחשב.',
    'קיבלתי. אם תרצה שאבצע משהו, נסח את זה כפקודה — למשל „פתח את כרום” או „בנה לי אתר”.',
    'שאלה טובה. במצב הדמיה אין לי חיבור למודל שפה, אבל בגרסה המלאה אענה כאן תשובה מלאה.',
  ],
  [
    'I’m here. Ask me to open apps, research topics, build projects or manage the computer.',
    'Got it. If you want me to do something, phrase it as a command — e.g. “open Chrome” or “build me a website”.',
    'Good question. The simulator has no language model, but the full version answers right here.',
  ],
);
