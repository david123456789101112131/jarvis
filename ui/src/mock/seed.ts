// Initial "lived-in" world for the simulator: finished tasks with full timelines, chat,
// activity, audit, memory, files, grants and settings.
import type {
  ActivityEntry,
  AgentId,
  AgentView,
  AuditEntry,
  ChatMessage,
  Grant,
  Lang,
  MemoryFact,
  MemoryProject,
  RecentFile,
  SecretStatus,
  SettingsObject,
  SubTaskView,
  TaskDetail,
  TaskStatus,
  TimelineEntry,
  UISettings,
} from '../api/types';
import { AGENT_PROFILES, L, LL, PATHS, TASK_TEMPLATES, pick, type Bi, type TaskTemplate } from './content';
import { hmsAt, isoAt, mockId, unique } from './util';

const MIN = 60_000;
const HOUR = 60 * MIN;

export function agentLabel(agent: AgentId, lang: Lang): string {
  const p = AGENT_PROFILES.find((a) => a.id === agent)!;
  return lang === 'he' ? p.name_he : p.name;
}

export function seedAgents(lang: Lang, now: number): AgentView[] {
  const stats: Record<AgentId, AgentView['stats']> = {
    research: { completed: 47, failed: 2, avg_duration_s: 38.5 },
    system: { completed: 132, failed: 4, avg_duration_s: 6.2 },
    development: { completed: 21, failed: 3, avg_duration_s: 142 },
    creative: { completed: 18, failed: 0, avg_duration_s: 64.3 },
    planner: { completed: 39, failed: 1, avg_duration_s: 12.8 },
  };
  const last: Record<AgentId, Bi> = {
    research: L('נקראו 9 ביקורות על אוזניות', 'Read 9 headphone reviews'),
    system: L('נפתח Google Chrome', 'Opened Google Chrome'),
    development: L('הורצו 6 בדיקות — כולן עברו', 'Ran 6 tests — all passed'),
    creative: L('נוצרה טבלת השוואה', 'Created a comparison table'),
    planner: L('נבנתה תוכנית של 4 שלבים', 'Built a 4-step plan'),
  };
  const errors: Partial<Record<AgentId, AgentView['errors']>> = {
    development: [{ at: isoAt(now - 26 * HOUR), message: pick(L('SyntaxError בשורה 42 ב-organize_downloads.py', 'SyntaxError on line 42 in organize_downloads.py'), lang) }],
    system: [{ at: isoAt(now - 5 * HOUR), message: pick(L('OneDrive לא מחובר — הגיבוי נכשל', 'OneDrive is not signed in — backup failed'), lang) }],
  };
  return AGENT_PROFILES.map((p, i) => ({
    id: p.id,
    number: p.number,
    name: p.name,
    name_he: p.name_he,
    role: p.role,
    role_he: p.role_he,
    status: 'idle',
    enabled: true,
    killed: false,
    current: null,
    progress: 0,
    last_action: pick(last[p.id], lang),
    last_action_at: isoAt(now - (12 + i * 9) * MIN),
    errors: errors[p.id] ?? [],
    permissions: p.permissions,
    queue: 0,
    stats: stats[p.id],
    skills: p.skills,
    ai: { preferred: 'xai', provider: 'xai', model: 'grok-4.7', when_told: true },
  }));
}

interface Outcome {
  status: TaskStatus;
  failAt?: string; // subtask key that fails / stops
  error?: Bi;
}

/** Build a finished task (plan, timeline, result) as if it had run at `start`. */
export function materializeTask(tpl: TaskTemplate, lang: Lang, start: number, outcome: Outcome, source: TaskDetail['source'] = 'voice'): TaskDetail {
  const id = mockId('task');
  const timeline: TimelineEntry[] = [];
  const tl = (ms: number, kind: string, message: Bi, agent?: AgentId, subtaskId?: string, level = 'info') =>
    timeline.push({ ts: isoAt(ms), time: hmsAt(ms), kind, message: pick(message, lang), agent_id: agent ?? null, subtask_id: subtaskId ?? null, level });

  const title = pick(tpl.title, lang);
  tl(start, 'received', L(`המשימה התקבלה: ${title}`, `Task received: ${title}`));
  tl(start + 2600, 'plan', L(`נבנתה תוכנית של ${tpl.subtasks.length} תתי-משימות`, `Plan ready: ${tpl.subtasks.length} subtasks`), 'planner');

  const ends = new Map<string, number>();
  const subtasks: SubTaskView[] = [];
  let stopped = false;
  let t = start + 3000;
  for (const st of tpl.subtasks) {
    const sid = `${id}.${st.key}`;
    const depsEnd = Math.max(start + 3000, ...st.depends.map((d) => ends.get(d) ?? start + 3000));
    const begin = depsEnd + 400;
    const failedHere = outcome.failAt === st.key;
    const dur = st.seconds * 1000 * (failedHere ? 0.55 : 1);
    const end = begin + dur;
    let status: TaskStatus = 'completed';
    if (stopped) status = outcome.status === 'interrupted' ? 'pending' : outcome.status === 'failed' ? 'skipped' : 'cancelled';
    else if (failedHere) status = outcome.status === 'interrupted' ? 'interrupted' : outcome.status;
    const ran = status !== 'pending' && status !== 'skipped' && !(stopped && status === 'cancelled');
    if (ran) {
      tl(begin, 'subtask.started', L(`${agentLabel(st.agent, 'he')} התחיל: ${pick(st.title, 'he')}`, `${agentLabel(st.agent, 'en')} started: ${pick(st.title, 'en')}`), st.agent, sid);
      st.actions[lang].forEach((a, i) => tl(begin + ((i + 1) * dur) / (st.actions[lang].length + 1), 'action', L(a, a), st.agent, sid));
      if (st.failOnce && status === 'completed') {
        tl(begin + dur * 0.4, 'subtask.retry', L(`ניסיון חוזר: ${pick(st.failOnce, 'he')}`, `Retrying: ${pick(st.failOnce, 'en')}`), st.agent, sid, 'warning');
      }
      if (status === 'completed') tl(end, 'subtask.completed', L(`${agentLabel(st.agent, 'he')} סיים: ${pick(st.title, 'he')}`, `${agentLabel(st.agent, 'en')} finished: ${pick(st.title, 'en')}`), st.agent, sid, 'success');
      else if (outcome.error) tl(end, `subtask.${status}`, outcome.error, st.agent, sid, status === 'failed' ? 'error' : 'warning');
    }
    ends.set(st.key, end);
    t = Math.max(t, end);
    subtasks.push({
      id: sid,
      agent: st.agent,
      title: pick(st.title, lang),
      instruction: pick(st.instruction, lang),
      status,
      progress: status === 'completed' ? 1 : ran ? 0.45 : 0,
      attempts: ran ? (st.failOnce && status === 'completed' ? 2 : 1) : 0,
      depends_on: st.depends.map((d) => `${id}.${d}`),
      optional: !!st.optional,
      started_at: ran ? isoAt(begin) : null,
      finished_at: ran && status !== 'interrupted' ? isoAt(end) : null,
      summary: status === 'completed' ? pick(st.summary, lang) : null,
      error: failedHere && outcome.error ? pick(outcome.error, lang) : null,
      files_created: status === 'completed' ? st.files ?? [] : [],
    });
    if (failedHere) stopped = true;
  }
  const finished = t + 1500;
  const done = outcome.status === 'completed';
  if (done) {
    tl(finished - 1400, 'verify', L('אימות התוצאות', 'Verifying results'));
    tl(finished, 'completed', L('המשימה הושלמה', 'Task completed'), undefined, undefined, 'success');
  } else if (outcome.status === 'cancelled') {
    tl(finished, 'cancelled', L('המשימה בוטלה על ידי המשתמש', 'Task cancelled by the user'), undefined, undefined, 'warning');
  } else if (outcome.status === 'failed') {
    tl(finished, 'failed', L('המשימה נכשלה', 'Task failed'), undefined, undefined, 'error');
  } else if (outcome.status === 'interrupted') {
    tl(finished, 'interrupted', L('המשימה נקטעה — JARVIS נסגר. אפשר להמשיך מאותה נקודה.', 'Task interrupted — JARVIS was closed. It can resume from here.'), undefined, undefined, 'warning');
  }
  const completedSubs = subtasks.filter((s) => s.status === 'completed');
  const files = completedSubs.flatMap((s) => s.files_created);
  const progress = subtasks.reduce((sum, s) => sum + (s.status === 'completed' ? 1 : s.progress), 0) / subtasks.length;
  return {
    id,
    title,
    goal: pick(tpl.goal, lang),
    status: outcome.status,
    progress: done ? 1 : progress,
    priority: tpl.priority,
    mode: 'normal',
    agents: unique(tpl.subtasks.map((s) => s.agent)),
    source,
    created_at: isoAt(start),
    updated_at: isoAt(finished),
    started_at: isoAt(start),
    finished_at: outcome.status === 'interrupted' ? null : isoAt(finished),
    summary: done ? pick(tpl.result.summary, lang) : outcome.error ? pick(outcome.error, lang) : null,
    error: done ? null : outcome.error ? pick(outcome.error, lang) : null,
    plan: { strategy: pick(tpl.strategy, lang), subtasks },
    timeline,
    result: done
      ? {
          summary: pick(tpl.result.summary, lang),
          details: tpl.result.details ? pick(tpl.result.details, lang) : null,
          files_created: files,
          actions_taken: tpl.result.actions[lang],
          issues: tpl.result.issues[lang],
          next_step: pick(tpl.result.next, lang),
        }
      : outcome.status === 'failed'
        ? {
            summary: outcome.error ? pick(outcome.error, lang) : '',
            details: null,
            files_created: files,
            actions_taken: completedSubs.map((s) => s.summary ?? s.title),
            issues: outcome.error ? [pick(outcome.error, lang)] : [],
            next_step: pick(L('להתחבר ל-OneDrive ולנסות שוב.', 'Sign in to OneDrive and retry.'), lang),
          }
        : null,
  };
}

const BACKUP: TaskTemplate = {
  key: 'backup',
  title: L('גיבוי הפרויקטים ל-OneDrive', 'Back up projects to OneDrive'),
  goal: L('לגבות את תיקיית הפרויקטים ל-OneDrive ולאמת את הגיבוי.', 'Back up the projects folder to OneDrive and verify it.'),
  strategy: L('תכנון → העתקה → אימות', 'Plan → copy → verify'),
  priority: 'normal',
  subtasks: [
    {
      key: 'plan',
      agent: 'planner',
      title: L('תכנון הגיבוי', 'Plan the backup'),
      instruction: L('בחר תיקיות ויעד.', 'Pick folders and destination.'),
      depends: [],
      seconds: 4,
      actions: LL(['נבחרו 4 תיקיות (2.3GB)'], ['Selected 4 folders (2.3 GB)']),
      summary: L('4 תיקיות, 2.3GB.', '4 folders, 2.3 GB.'),
    },
    {
      key: 'copy',
      agent: 'system',
      title: L('העתקה ל-OneDrive', 'Copy to OneDrive'),
      instruction: L('העתק את התיקיות.', 'Copy the folders.'),
      depends: ['plan'],
      seconds: 9,
      actions: LL(['בדיקת חיבור ל-OneDrive'], ['Checked the OneDrive connection']),
      summary: L('', ''),
    },
    {
      key: 'verify',
      agent: 'system',
      title: L('אימות הגיבוי', 'Verify the backup'),
      instruction: L('השווה גדלים ומספר קבצים.', 'Compare sizes and file counts.'),
      depends: ['copy'],
      seconds: 4,
      actions: LL([], []),
      summary: L('', ''),
    },
  ],
  result: { summary: L('', ''), details: null, actions: LL([], []), issues: LL([], []), next: L('', '') },
};

const SLIDES: TaskTemplate = {
  key: 'slides',
  title: L('מצגת על מגמות AI ל-2026', 'Presentation on AI trends for 2026'),
  goal: L('להכין מצגת של 10 שקפים על מגמות AI.', 'Prepare a 10-slide deck on AI trends.'),
  strategy: L('מחקר → כתיבה → עיצוב', 'Research → writing → design'),
  priority: 'low',
  subtasks: [
    {
      key: 'research',
      agent: 'research',
      title: L('מחקר מגמות', 'Research the trends'),
      instruction: L('אסוף 8 מגמות עם מקורות.', 'Collect 8 trends with sources.'),
      depends: [],
      seconds: 10,
      actions: LL(['נקראו 7 דוחות'], ['Read 7 reports']),
      summary: L('8 מגמות עם מקורות.', '8 trends with sources.'),
    },
    {
      key: 'deck',
      agent: 'creative',
      title: L('עיצוב השקפים', 'Design the slides'),
      instruction: L('10 שקפים בעיצוב נקי.', '10 clean slides.'),
      depends: ['research'],
      seconds: 12,
      actions: LL(['נבחרה תבנית'], ['Picked a template']),
      summary: L('', ''),
    },
  ],
  result: { summary: L('', ''), details: null, actions: LL([], []), issues: LL([], []), next: L('', '') },
};

const TRANSLATE: TaskTemplate = {
  key: 'translate',
  title: L('תרגום מדריך המשתמש לאנגלית', 'Translate the user guide to English'),
  goal: L('לתרגם את מדריך המשתמש (14 עמודים) לאנגלית ולשמור כ-PDF.', 'Translate the 14-page user guide to English and save it as PDF.'),
  strategy: L('חלוקה לפרקים → תרגום → עימוד', 'Split into chapters → translate → typeset'),
  priority: 'normal',
  subtasks: [
    {
      key: 'split',
      agent: 'planner',
      title: L('חלוקה לפרקים', 'Split into chapters'),
      instruction: L('חלק את המדריך ל-5 פרקים.', 'Split the guide into 5 chapters.'),
      depends: [],
      seconds: 4,
      actions: LL(['זוהו 5 פרקים'], ['Found 5 chapters']),
      summary: L('5 פרקים.', '5 chapters.'),
    },
    {
      key: 'translate',
      agent: 'creative',
      title: L('תרגום הפרקים', 'Translate the chapters'),
      instruction: L('תרגם בשפה ברורה.', 'Translate in plain language.'),
      depends: ['split'],
      seconds: 16,
      actions: LL(['תורגמו פרקים 1–2'], ['Translated chapters 1–2']),
      summary: L('', ''),
      files: [`${PATHS.workspace}\\user-guide-en.md`],
    },
    {
      key: 'pdf',
      agent: 'development',
      title: L('יצירת PDF', 'Generate the PDF'),
      instruction: L('עמד ושמור כ-PDF.', 'Typeset and save as PDF.'),
      depends: ['translate'],
      seconds: 6,
      actions: LL([], []),
      summary: L('', ''),
    },
  ],
  result: { summary: L('', ''), details: null, actions: LL([], []), issues: LL([], []), next: L('', '') },
};

export function seedTasks(lang: Lang, now: number): TaskDetail[] {
  return [
    materializeTask(TASK_TEMPLATES[1], lang, now - 2 * HOUR - 14 * MIN, { status: 'completed' }, 'voice'),
    materializeTask(BACKUP, lang, now - 5 * HOUR, { status: 'failed', failAt: 'copy', error: L('OneDrive לא מחובר — הגיבוי נכשל', 'OneDrive is not signed in — backup failed') }, 'text'),
    materializeTask(TRANSLATE, lang, now - 9 * HOUR, { status: 'interrupted', failAt: 'translate', error: L('JARVIS נסגר באמצע התרגום', 'JARVIS was closed during translation') }, 'text'),
    materializeTask(TASK_TEMPLATES[2], lang, now - 26 * HOUR, { status: 'completed' }, 'text'),
    materializeTask(SLIDES, lang, now - 30 * HOUR, { status: 'cancelled', failAt: 'deck', error: L('בוטל על ידי המשתמש', 'Cancelled by the user') }, 'voice'),
  ];
}

export function seedChat(lang: Lang, now: number): ChatMessage[] {
  const msg = (offset: number, role: ChatMessage['role'], kind: ChatMessage['kind'], text: Bi, source: ChatMessage['source'] = 'text'): ChatMessage => ({
    id: mockId('msg'),
    role,
    text: pick(text, lang),
    ts: isoAt(now - offset),
    time: hmsAt(now - offset),
    source,
    language: lang,
    kind,
    task_id: null,
    intent: null,
  });
  return [
    msg(2 * HOUR + 15 * MIN, 'user', 'answer', L('השווה לי אוזניות מבטלות רעשים עד 1500 שקל', 'Compare noise-cancelling headphones under ₪1,500 for me'), 'voice'),
    msg(2 * HOUR + 15 * MIN - 1500, 'jarvis', 'ack', L('קיבלתי. אני מחלק את המשימה בין הסוכנים.', 'On it. I’m splitting the task between the agents.')),
    msg(2 * HOUR + 14 * MIN - 50_000, 'jarvis', 'result', L(
      'ההמלצה: **Sony WH-1000XM5** — הכי טובה בביטול רעשים, ב-₪1,290.\n\n- Bose QC Ultra — ₪1,450\n- Soundcore Space One — ₪890\n\nהטבלה המלאה שמורה ב-`headphones-comparison.html`.',
      'My pick: **Sony WH-1000XM5** — best noise cancelling, at ₪1,290.\n\n- Bose QC Ultra — ₪1,450\n- Soundcore Space One — ₪890\n\nThe full table is saved as `headphones-comparison.html`.',
    )),
    msg(40 * MIN, 'user', 'answer', L('מה מצב המערכת?', 'How is the system doing?')),
    msg(40 * MIN - 900, 'jarvis', 'answer', L('המעבד ב-14 אחוז, הזיכרון ב-55 אחוז ובדיסק C נשארו 372GB פנויים. הכול תקין.', 'CPU is at 14%, memory at 55%, and drive C has 372 GB free. All good.')),
  ];
}

export function seedActivity(lang: Lang, now: number, tasks: TaskDetail[]): ActivityEntry[] {
  const out: ActivityEntry[] = [];
  const add = (ms: number, message: Bi, level: ActivityEntry['level'] = 'info', agent: AgentId | null = null, kind = 'system') =>
    out.push({
      id: mockId('act'),
      ts: isoAt(ms),
      time: hmsAt(ms),
      source: agent ? agentLabel(agent, lang) : 'JARVIS',
      message: pick(message, lang),
      level,
      agent_id: agent,
      task_id: null,
      kind,
    });
  const boot = now - 3 * HOUR;
  add(boot, L('JARVIS עלה (גרסה 1.0.0)', 'JARVIS started (version 1.0.0)'), 'success');
  add(boot + 900, L('5 סוכנים מחוברים', '5 agents online'), 'success');
  add(boot + 1400, L('הקול מוכן: openWakeWord · faster-whisper · Edge TTS', 'Voice ready: openWakeWord · faster-whisper · Edge TTS'), 'info');
  add(boot + 1800, L('ספק AI פעיל: Anthropic', 'Active AI provider: Anthropic'), 'info');
  for (const task of tasks.slice(0, 3)) {
    for (const e of task.timeline) {
      out.push({
        id: mockId('act'),
        ts: e.ts,
        time: e.time,
        source: e.agent_id ? agentLabel(e.agent_id as AgentId, lang) : 'JARVIS',
        message: e.message,
        level: (e.level as ActivityEntry['level']) ?? 'info',
        agent_id: e.agent_id ?? null,
        task_id: task.id,
        kind: e.kind,
      });
    }
  }
  add(now - 40 * MIN, L('בדיקת מצב מערכת: תקין', 'System status check: OK'), 'success', 'system');
  add(now - 12 * MIN, L('נפתח Google Chrome', 'Opened Google Chrome'), 'success', 'system');
  return out.filter((e) => Date.parse(e.ts) <= now).sort((a, b) => (a.ts < b.ts ? -1 : 1)).slice(-160);
}

export function seedAudit(now: number): AuditEntry[] {
  const rows: [number, AgentId, string, string, string, string, string, string, string, string | null, number | null][] = [
    [3, 'system', 'process.open', 'process.launch', 'Open Google Chrome', 'low', 'allowed', 'ok', '', null, 412],
    [8, 'system', 'system.status', 'system.info', 'Read CPU, memory and disk usage', 'safe', 'allowed', 'ok', '', null, 38],
    [40, 'research', 'web.search', 'net.search', 'Search: noise cancelling headphones review 2026', 'low', 'allowed', 'ok', '', null, 1840],
    [41, 'research', 'web.fetch', 'net.fetch', 'Fetch rtings.com/headphones/reviews', 'low', 'allowed', 'ok', '', null, 960],
    [43, 'creative', 'fs.write_file', 'fs.write', 'Write workspace\\headphones-comparison.html', 'low', 'allowed', 'ok', '', null, 22],
    [95, 'system', 'shell.run', 'shell.execute', 'robocopy projects → OneDrive (2.3 GB)', 'high', 'confirmed', 'error', 'Copies many files outside the workspace', 'OneDrive is not signed in', 3021],
    [100, 'development', 'net.api', 'net.api', 'POST api.github.com with request body', 'medium', 'blocked', 'skipped', 'Payload contained a secret', null, null],
    [180, 'development', 'code.run_tests', 'code.execute', 'pytest test_organize.py', 'medium', 'allowed', 'ok', 'Runs code', null, 4210],
    [182, 'system', 'fs.move', 'fs.write', 'Move 214 files in Downloads', 'high', 'confirmed', 'ok', 'Moves many files', null, 2210],
    [185, 'system', 'fs.delete', 'fs.delete', 'Delete Downloads\\Temp (37 files)', 'high', 'rejected', 'skipped', 'Broad deletion', null, null],
    [190, 'planner', 'memory.write', 'memory.write', 'Save fact: budget for gadgets', 'safe', 'allowed', 'ok', '', null, 9],
    [200, 'research', 'web.search', 'net.search', 'Search: python organize files by extension', 'low', 'allowed', 'ok', '', null, 1320],
  ];
  return rows.map(([minAgo, agent, tool, capability, action, risk, decision, outcome, reason, error, duration]) => ({
    id: mockId('aud'),
    ts: isoAt(now - minAgo * MIN),
    agent_id: agent,
    task_id: null,
    tool,
    capability,
    action,
    risk,
    decision,
    outcome,
    reason,
    error,
    duration_ms: duration,
  }));
}

export function seedFacts(lang: Lang, now: number): MemoryFact[] {
  const facts: [Bi, string, number][] = [
    [L('שם המשתמשת: דנה', 'The user’s name is Dana'), 'identity', 30 * 24],
    [L('מעדיפה תשובות קצרות בעברית', 'Prefers short answers in Hebrew'), 'preference', 20 * 24],
    [L('בית הקפה נקרא „אורה” ונמצא בתל אביב', 'The coffee shop is called “Ora” and is in Tel Aviv'), 'business', 6 * 24],
    [L('תקציב לגאדג׳טים: עד ₪1,500', 'Gadget budget: up to ₪1,500'), 'preference', 3],
    [L('עובדת עם VS Code ו-Python 3.12', 'Works with VS Code and Python 3.12'), 'dev', 12 * 24],
    [L('אוהבת עיצובים כהים ומינימליסטיים', 'Likes dark, minimal designs'), 'preference', 9 * 24],
  ];
  return facts.map(([text, category, hoursAgo]) => ({ id: mockId('fact'), text: pick(text, lang), category, created_at: isoAt(now - hoursAgo * HOUR) }));
}

export function seedProjects(lang: Lang, now: number): MemoryProject[] {
  return [
    { id: mockId('proj'), name: 'ora-coffee', path: `${PATHS.projects}\\ora-coffee`, description: pick(L('דף נחיתה לבית הקפה אורה', 'Landing page for Ora coffee shop'), lang), updated_at: isoAt(now - 20 * MIN), status: 'active' },
    { id: mockId('proj'), name: 'downloads-organizer', path: `${PATHS.projects}\\downloads-organizer`, description: pick(L('סקריפט Python לסידור ההורדות', 'Python script that organizes Downloads'), lang), updated_at: isoAt(now - 26 * HOUR), status: 'completed' },
    { id: mockId('proj'), name: 'user-guide', path: `${PATHS.projects}\\user-guide`, description: pick(L('מדריך משתמש — תרגום לאנגלית', 'User guide — English translation'), lang), updated_at: isoAt(now - 9 * HOUR), status: 'paused' },
    { id: mockId('proj'), name: 'jarvis-notes', path: `${PATHS.projects}\\jarvis-notes`, description: pick(L('הערות ורעיונות לשיפורים', 'Notes and ideas for improvements'), lang), updated_at: isoAt(now - 4 * 24 * HOUR), status: 'active' },
  ];
}

export function seedFiles(now: number): RecentFile[] {
  return [
    { path: `${PATHS.workspace}\\headphones-comparison.html`, name: 'headphones-comparison.html', size: 18_432, task_id: null, agent_id: 'creative', created_at: isoAt(now - 2 * HOUR) },
    { path: `${PATHS.workspace}\\user-guide-en.md`, name: 'user-guide-en.md', size: 41_870, task_id: null, agent_id: 'creative', created_at: isoAt(now - 9 * HOUR) },
    { path: `${PATHS.projects}\\downloads-organizer\\organize_downloads.py`, name: 'organize_downloads.py', size: 6_214, task_id: null, agent_id: 'development', created_at: isoAt(now - 26 * HOUR) },
    { path: `${PATHS.projects}\\downloads-organizer\\test_organize.py`, name: 'test_organize.py', size: 3_102, task_id: null, agent_id: 'development', created_at: isoAt(now - 26 * HOUR) },
  ];
}

export function seedGrants(now: number): Grant[] {
  return [
    {
      id: mockId('grant'),
      agent_id: 'research',
      capability: 'fs.write',
      roots: ['C:\\Users\\Dana\\Documents\\Research'],
      task_id: null,
      expires_at: isoAt(now + 6 * HOUR),
      max_uses: null,
      uses: 4,
      source: 'user',
      reason: 'Save research notes to Documents',
      created_at: isoAt(now - 2 * HOUR),
    },
    {
      id: mockId('grant'),
      agent_id: 'development',
      capability: 'net.api',
      roots: [],
      task_id: null,
      expires_at: isoAt(now + 2 * HOUR),
      max_uses: 20,
      uses: 3,
      source: 'jarvis',
      reason: 'GitHub API for the ora-coffee repository',
      created_at: isoAt(now - 30 * MIN),
    },
  ];
}

export function seedSecrets(): Record<string, SecretStatus> {
  return {
    ANTHROPIC_API_KEY: { label: 'Anthropic (Claude) API key', configured: true, source: 'keyring' },
    XAI_API_KEY: { label: 'xAI (Grok) API key', configured: true, source: 'keyring' },
    OPENAI_API_KEY: { label: 'OpenAI API key', configured: false, source: null },
    LOCAL_MODEL_API_KEY: { label: 'Local model server API key (optional)', configured: false, source: null },
    BRAVE_API_KEY: { label: 'Brave Search API key (optional)', configured: false, source: null },
    TAVILY_API_KEY: { label: 'Tavily Search API key (optional)', configured: true, source: 'env' },
    PICOVOICE_ACCESS_KEY: { label: 'Picovoice access key for the Porcupine wake word (optional)', configured: false, source: null },
    GITHUB_TOKEN: { label: 'GitHub token for the Development agent (optional)', configured: false, source: null },
  };
}

export function seedSettings(ui: UISettings, lang: Lang): SettingsObject {
  return {
    general: { assistant_name: 'JARVIS', user_name: lang === 'he' ? 'דנה' : 'Dana', language: 'auto', default_mode: 'normal', resume_interrupted_on_start: false, start_with_windows: true },
    voice: {
      enabled: true,
      wake_word_engine: 'auto',
      wake_word_model: 'hey_jarvis',
      stt_engine: 'auto',
      stt_model: 'small',
      stt_language: 'auto',
      tts_engine: 'auto',
      tts_voice_he: 'he-IL-AvriNeural',
      tts_voice_en: 'en-GB-RyanNeural',
      speak_responses: true,
      always_listening: true,
      ctrl_to_talk: true,
      input_device: '',
      output_device: '',
    },
    ui: { ...ui, window_width: 1480, window_height: 920 },
    ai: {
      provider: 'auto',
      cloud_only_when_told: true,
      agent_providers: { research: 'xai', system: 'xai', development: 'xai', creative: 'xai', planner: 'xai' },
      anthropic: { model: 'claude-opus-5-5', fallbacks: true },
      xai: { model: 'grok-4.7' },
      openai: { model: 'gpt-5' },
      local: { enabled: false, base_url: 'http://127.0.0.1:11434/v1', model: 'llama3.1' },
    },
    security: { confirm_timeout_s: 120, network_enabled: true, shell_enabled: true },
  };
}
