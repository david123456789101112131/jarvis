import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { ACTIVE_TASK_STATUSES, type SubTaskView, type TaskDetail, type TaskStatus, type TaskSummary, type TimelineEntry } from '../api/types';
import { translate, useI18n, type MessageKey } from '../i18n';
import { durationBetween, fileName, formatDateTime, formatDuration, formatRelative, textDir } from '../lib/format';
import { useDebounced } from '../lib/hooks';
import { useApi, useAppState, useDispatch, useNow, useResource, useRunner } from '../state/context';
import { mergeTaskLists, planLevels } from '../state/selectors';
import { Icon } from '../components/Icon';
import { Markdown } from '../components/Markdown';
import { ScreenHeader } from '../components/shell/Shell';
import { AGENT_NUMBERS, SubtaskStateIcon, TaskStatusChip } from '../components/tasks';
import { LEVEL_TONE, TASK_TONE } from '../components/tones';
import { AgentBadge, Chip, ConfirmButton, EmptyState, ErrorBox, Loading, Panel, ProgressBar, Segmented } from '../components/ui';
import { BidiText } from '../components/BidiText';

type Filter = 'all' | 'active' | 'completed' | 'failed' | 'cancelled' | 'interrupted';
const FILTERS: Filter[] = ['all', 'active', 'completed', 'failed', 'cancelled', 'interrupted'];
const REPLAY_STEP_MS = 650;

// Timeline entry kinds written by the orchestrator (`_note`).
const TIMELINE_ICONS: Record<string, string> = {
  received: 'send',
  planning: 'compass',
  plan: 'list',
  plan_fallback: 'list',
  assign: 'agents',
  done: 'check',
  retry: 'restart',
  fallback: 'restart',
  failed: 'x',
  error: 'alert',
  timeout: 'clock',
  verify: 'eye',
  verify_failed: 'alert',
  followup: 'plus',
  help: 'info',
  confirm: 'shield',
  loop_guard: 'octagon',
  completed: 'check-circle',
  cancelled: 'stop',
  interrupted: 'pause',
  paused: 'pause',
  resume: 'replay',
  debug: 'terminal',
};

/** Timeline messages usually name their agent already ("Planner Agent finished: …"), in either language. */
function mentionsAgent(message: string, agentId: string): boolean {
  const key = `agent.name.${agentId}` as MessageKey;
  return [translate('he', key), translate('en', key)].some((name) => name !== key && message.includes(name));
}

function matches(task: TaskSummary, filter: Filter, query: string): boolean {
  if (filter === 'active' && !ACTIVE_TASK_STATUSES.includes(task.status)) return false;
  if (filter !== 'all' && filter !== 'active' && task.status !== filter) return false;
  if (!query) return true;
  const q = query.toLowerCase();
  return task.title.toLowerCase().includes(q) || task.goal.toLowerCase().includes(q) || task.id.toLowerCase().includes(q);
}

function TaskRow({ task, active, now, onSelect }: { task: TaskSummary; active: boolean; now: number; onSelect: () => void }) {
  const { lang } = useI18n();
  const live = ACTIVE_TASK_STATUSES.includes(task.status);
  return (
    <li>
      <button type="button" className="task-row" aria-current={active ? 'true' : undefined} data-status={task.status} onClick={onSelect} data-testid="tasks-row" data-task-id={task.id}>
        <span className="task-row__top">
          <TaskStatusChip status={task.status} />
          <span className="task-row__time">{formatRelative(task.created_at, lang, now)}</span>
        </span>
        <span className="task-row__title bidi" dir={textDir(task.title)}>
          {task.title}
        </span>
        <span className="task-row__foot">
          <span className="task-row__agents">
            {task.agents.map((a) => (
              <AgentBadge key={a} agent={a} number={AGENT_NUMBERS[a] ?? '•'} size="sm" />
            ))}
          </span>
          {live ? <ProgressBar value={task.progress} tone={TASK_TONE[task.status]} variant="thin" label={task.title} /> : null}
          <span className="task-row__pct num">{Math.round(task.progress * 100)}%</span>
        </span>
      </button>
    </li>
  );
}

function SubtaskCard({ subtask, titles, onOpenFile }: { subtask: SubTaskView; titles: Map<string, string>; onOpenFile: (path: string, reveal: boolean) => void }) {
  const { t, lang, agentName } = useI18n();
  const running = subtask.status === 'running' || subtask.status === 'verifying';
  const duration = durationBetween(subtask.started_at, subtask.finished_at);
  return (
    <li className="plan-step" data-status={subtask.status} data-testid="plan-step">
      <div className="plan-step__head">
        <AgentBadge agent={subtask.agent} number={AGENT_NUMBERS[subtask.agent] ?? '•'} size="sm" />
        <span className="plan-step__agent truncate">{agentName(subtask.agent)}</span>
        <SubtaskStateIcon subtask={subtask} />
      </div>
      <p className="plan-step__title bidi" dir={textDir(subtask.title)}>
        {subtask.title}
      </p>
      {running ? <ProgressBar value={subtask.progress} tone={TASK_TONE[subtask.status]} variant="thin" label={subtask.title} /> : null}
      <div className="plan-step__meta">
        <span>{t(`taskStatus.${subtask.status}` as MessageKey)}</span>
        {duration !== null && subtask.started_at ? <span className="num">{formatDuration(duration, lang)}</span> : null}
        {subtask.attempts > 1 ? <span>{t('task.attempt', { n: subtask.attempts })}</span> : null}
        {subtask.optional ? <Chip tone="muted">{t('task.optional')}</Chip> : null}
      </div>
      {subtask.depends_on.length ? (
        <p className="plan-step__deps truncate" title={subtask.depends_on.map((d) => titles.get(d) ?? d).join(', ')}>
          {t('task.after')}: {subtask.depends_on.map((d) => titles.get(d) ?? d).join(', ')}
        </p>
      ) : null}
      {subtask.error ? (
        <p className="plan-step__error bidi" dir={textDir(subtask.error)}>
          {subtask.error}
        </p>
      ) : subtask.summary ? (
        <p className="plan-step__summary bidi" dir={textDir(subtask.summary)}>
          {subtask.summary}
        </p>
      ) : null}
      {subtask.files_created.length ? (
        <ul className="plan-step__files">
          {subtask.files_created.map((path) => (
            <li key={path}>
              <button type="button" className="link-btn link-btn--sm ltr" onClick={() => onOpenFile(path, false)} title={path}>
                <Icon name="file" size={11} />
                {fileName(path)}
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </li>
  );
}

function PlanView({ detail, onOpenFile }: { detail: TaskDetail; onOpenFile: (path: string, reveal: boolean) => void }) {
  const { t } = useI18n();
  const subtasks = detail.plan?.subtasks ?? [];
  const levels = useMemo(() => planLevels(subtasks), [subtasks]);
  const titles = useMemo(() => new Map(subtasks.map((s) => [s.id, s.title])), [subtasks]);
  if (!detail.plan || !subtasks.length) {
    return <p className="hint">{ACTIVE_TASK_STATUSES.includes(detail.status) ? t('tasks.planning') : t('task.noPlan')}</p>;
  }
  return (
    <>
      {detail.plan.strategy ? (
        <p className="plan__strategy bidi" dir={textDir(detail.plan.strategy)}>
          <span className="faint">{t('task.strategy')}: </span>
          {detail.plan.strategy}
        </p>
      ) : null}
      <div className="plan" data-testid="task-plan">
        {levels.map((level, i) => (
          <div key={i} className="plan__level">
            <p className="plan__level-label">{t('task.stage', { n: i + 1 })}</p>
            <ol className="plan__steps">
              {level.map((s) => (
                <SubtaskCard key={s.id} subtask={s} titles={titles} onOpenFile={onOpenFile} />
              ))}
            </ol>
          </div>
        ))}
      </div>
    </>
  );
}

function TimelineView({ entries }: { entries: TimelineEntry[] }) {
  const { t, agentName } = useI18n();
  const [shown, setShown] = useState<number | null>(null); // null = everything (not replaying)
  const [playing, setPlaying] = useState(false);
  useEffect(() => {
    if (!playing) return;
    const id = setInterval(() => {
      setShown((n) => {
        const next = (n ?? 0) + 1;
        if (next >= entries.length) {
          setPlaying(false);
          return null;
        }
        return next;
      });
    }, REPLAY_STEP_MS);
    return () => clearInterval(id);
  }, [playing, entries.length]);
  const visible = shown === null ? entries : entries.slice(0, shown + 1);
  return (
    <>
      <div className="timeline__controls">
        {playing ? (
          <button type="button" className="btn btn--sm" onClick={() => setPlaying(false)} data-testid="timeline-pause">
            <Icon name="pause" size={13} />
            {t('task.pause')}
          </button>
        ) : (
          <button
            type="button"
            className="btn btn--sm"
            disabled={entries.length < 2}
            onClick={() => {
              setShown((n) => (n === null ? 0 : n));
              setPlaying(true);
            }}
            data-testid="timeline-replay"
          >
            <Icon name="replay" size={13} />
            {t('task.replay')}
          </button>
        )}
        {shown !== null ? (
          <button
            type="button"
            className="btn btn--sm btn--ghost"
            onClick={() => {
              setPlaying(false);
              setShown(null);
            }}
          >
            {t('task.showAll')}
          </button>
        ) : null}
        {shown !== null ? <span className="num faint">{`${shown + 1}/${entries.length}`}</span> : null}
      </div>
      {visible.length ? (
        <ol className="timeline" data-testid="task-timeline">
          {visible.map((entry, i) => (
            <li key={`${entry.ts}-${i}`} className="timeline__item" data-kind={entry.kind} data-tone={LEVEL_TONE[entry.level ?? 'info'] ?? 'steel'} data-fresh={shown !== null && i === visible.length - 1 ? 'true' : undefined}>
              <span className="timeline__dot" aria-hidden="true">
                <Icon name={TIMELINE_ICONS[entry.kind] ?? 'dots'} size={12} />
              </span>
              <span className="timeline__time num ltr">{entry.time}</span>
              <span className="timeline__msg bidi" dir={textDir(entry.message)}>
                {entry.agent_id && !mentionsAgent(entry.message, entry.agent_id) ? <span className="timeline__agent">{agentName(entry.agent_id)} · </span> : null}
                <BidiText text={entry.message} />
              </span>
            </li>
          ))}
        </ol>
      ) : (
        <p className="hint">{t('task.noTimeline')}</p>
      )}
    </>
  );
}

/** Matches `.result__details.is-clipped { max-height }`. */
const DETAILS_CLIP_PX = 240;

/** Long result details start clipped with a "show all" button; short ones are shown whole. */
function ResultDetails({ text }: { text: string }) {
  const { t } = useI18n();
  const ref = useRef<HTMLDivElement>(null);
  const [long, setLong] = useState(false);
  const [expanded, setExpanded] = useState(false);
  useLayoutEffect(() => {
    if (ref.current && !long) setLong(ref.current.scrollHeight > DETAILS_CLIP_PX + 24);
  }, [text, long]);
  const clipped = long && !expanded;
  return (
    <div ref={ref} className={`result__details ${clipped ? 'is-clipped' : ''}`} data-testid="task-result-details">
      <Markdown text={text} dir={textDir(text)} />
      {clipped ? (
        <button type="button" className="result__more btn btn--sm" onClick={() => setExpanded(true)}>
          <Icon name="chevron-down" size={13} />
          {t('task.showAll')}
        </button>
      ) : null}
    </div>
  );
}

function ResultView({ detail, onOpenFile }: { detail: TaskDetail; onOpenFile: (path: string, reveal: boolean) => void }) {
  const { t } = useI18n();
  const result = detail.result;
  if (!result) {
    if (detail.error) return <ErrorBox message={detail.error} />;
    return <p className="hint">{detail.summary ?? t('task.noResult')}</p>;
  }
  return (
    <div className="result" data-testid="task-result">
      <Markdown text={result.summary} className="result__summary" dir={textDir(result.summary)} />
      {result.details ? <ResultDetails key={detail.id} text={result.details} /> : null}
      {result.files_created.length ? (
        <section className="result__section">
          <p className="section-label">{t('task.files')}</p>
          <ul className="file-list">
            {result.files_created.map((path) => (
              <li key={path} className="file-row">
                <button type="button" className="file-row__open" onClick={() => onOpenFile(path, false)} title={path}>
                  <Icon name="file" size={14} />
                  <span className="file-row__name truncate ltr">{fileName(path)}</span>
                  <span className="file-row__meta truncate ltr">{path}</span>
                </button>
                <button type="button" className="icon-btn icon-btn--sm" onClick={() => onOpenFile(path, true)} aria-label={t('files.reveal')} title={t('files.reveal')}>
                  <Icon name="folder" size={13} />
                </button>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      {result.actions_taken.length ? (
        <section className="result__section">
          <p className="section-label">{t('task.actions')}</p>
          <ul className="bullet-list">
            {result.actions_taken.map((a, i) => (
              <li key={i} className="bidi">
                <BidiText text={a} />
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      {result.issues.length ? (
        <section className="result__section">
          <p className="section-label">{t('task.issues')}</p>
          <ul className="bullet-list bullet-list--warn">
            {result.issues.map((a, i) => (
              <li key={i} className="bidi">
                <BidiText text={a} />
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      {result.next_step ? (
        <section className="result__section">
          <p className="section-label">{t('task.nextStep')}</p>
          <p className="bidi">
            <BidiText text={result.next_step} />
          </p>
        </section>
      ) : null}
    </div>
  );
}

function TaskActions({ task }: { task: TaskSummary }) {
  const { t } = useI18n();
  const api = useApi();
  const dispatch = useDispatch();
  const globalKill = useAppState((s) => s.killSwitch.global);
  const { run, isPending } = useRunner(t('common.requestFailed'));
  const apply = (updated: TaskSummary | undefined) => {
    if (updated?.id) dispatch({ type: 'event', event: { type: 'task.updated', data: updated }, at: Date.now() });
  };
  const active = ACTIVE_TASK_STATUSES.includes(task.status);
  return (
    <>
      {active ? (
        <ConfirmButton className="btn btn--sm btn--danger" confirmLabel={t('common.confirm')} onConfirm={() => void run('cancel', async () => apply(await api.cancelTask(task.id)))} disabled={isPending('cancel')} testId="task-detail-cancel">
          <Icon name="x" size={13} />
          {t('task.cancel')}
        </ConfirmButton>
      ) : null}
      {task.status === 'interrupted' || task.status === 'cancelled' ? (
        <button type="button" className="btn btn--sm btn--ok" disabled={globalKill || isPending('resume')} title={globalKill ? t('kill.engagedHint') : undefined} onClick={() => void run('resume', async () => apply(await api.resumeTask(task.id)))} data-testid="task-resume">
          <Icon name="play" size={13} />
          {t('task.resume')}
        </button>
      ) : null}
      {task.status === 'failed' ? (
        <button type="button" className="btn btn--sm" disabled={globalKill || isPending('retry')} title={globalKill ? t('kill.engagedHint') : undefined} onClick={() => void run('retry', async () => apply(await api.retryTask(task.id)))} data-testid="task-retry">
          <Icon name="restart" size={13} />
          {t('task.retry')}
        </button>
      ) : null}
    </>
  );
}

function TaskDetailView({ id, onClose }: { id: string; onClose: () => void }) {
  const { t, lang, agentName } = useI18n();
  const api = useApi();
  const dispatch = useDispatch();
  const stored = useAppState((s) => s.taskDetails[id]);
  const now = useNow(1000, !!stored && ACTIVE_TASK_STATUSES.includes(stored.status));
  const epoch = useAppState((s) => s.connection.connectedAt);
  const fetched = useResource(
    async (a) => {
      const detail = await a.getTask(id);
      dispatch({ type: 'task.detail', detail });
      return detail;
    },
    [id, epoch],
  );
  const { run } = useRunner(t('common.requestFailed'));
  const openFile = (path: string, reveal: boolean) => void run(`open:${path}`, () => api.openFile(path, reveal));
  const detail = stored ?? fetched.data;

  if (!detail) {
    if (fetched.error) {
      return (
        <Panel className="task-detail" title={t('nav.tasks')} icon="tasks" actions={<CloseButton onClose={onClose} />}>
          <EmptyState icon="alert" title={t('task.notFound')} hint={fetched.error} />
        </Panel>
      );
    }
    return (
      <Panel className="task-detail" title={t('task.loading')} icon="tasks">
        <Loading label={t('task.loading')} rows={6} />
      </Panel>
    );
  }

  const duration = durationBetween(detail.started_at ?? detail.created_at, detail.finished_at, now);
  const live = ACTIVE_TASK_STATUSES.includes(detail.status);
  return (
    <Panel
      className="task-detail"
      title={<span className="mono ltr">{detail.id}</span>}
      icon="tasks"
      actions={
        <>
          <TaskActions task={detail} />
          <CloseButton onClose={onClose} />
        </>
      }
      testId="task-detail"
    >
      <header className="task-hero">
        <TaskStatusChip status={detail.status} size="lg" />
        <h2 className="task-hero__title bidi" dir={textDir(detail.title)}>
          {detail.title}
        </h2>
        {detail.goal && detail.goal !== detail.title ? (
          <p className="task-hero__goal bidi" dir={textDir(detail.goal)}>
            <span className="faint">{t('task.goal')}: </span>
            {detail.goal}
          </p>
        ) : null}
        <div className="task-hero__progress">
          <ProgressBar value={detail.progress} tone={TASK_TONE[detail.status]} variant="thick" indeterminate={live && detail.progress === 0} label={detail.title} />
          <span className="num">{Math.round(detail.progress * 100)}%</span>
        </div>
        <dl className="task-meta">
          <div>
            <dt>{t('task.created')}</dt>
            <dd className="num">{formatDateTime(detail.created_at, lang)}</dd>
          </div>
          <div>
            <dt>{t('task.duration')}</dt>
            <dd className="num">{duration !== null ? formatDuration(duration, lang) : '—'}</dd>
          </div>
          <div>
            <dt>{t('task.mode')}</dt>
            <dd>{t(`mode.${detail.mode}` as MessageKey)}</dd>
          </div>
          <div>
            <dt>{t('task.priority')}</dt>
            <dd>{t(`priority.${detail.priority}` as MessageKey)}</dd>
          </div>
          <div>
            <dt>{t('task.source')}</dt>
            <dd>{t(`source.${detail.source}` as MessageKey)}</dd>
          </div>
          <div>
            <dt>{t('task.agents')}</dt>
            <dd className="task-meta__agents">
              {detail.agents.map((a) => (
                <span key={a} title={agentName(a)}>
                  <AgentBadge agent={a} number={AGENT_NUMBERS[a] ?? '•'} size="sm" />
                </span>
              ))}
            </dd>
          </div>
        </dl>
      </header>

      <section className="task-section">
        <p className="section-label">{t('task.plan')}</p>
        <PlanView detail={detail} onOpenFile={openFile} />
      </section>
      <section className="task-section">
        <p className="section-label">{t('task.result')}</p>
        <ResultView detail={detail} onOpenFile={openFile} />
      </section>
      <section className="task-section">
        <p className="section-label">{t('task.timeline')}</p>
        <TimelineView key={detail.id} entries={detail.timeline} />
      </section>
    </Panel>
  );
}

function CloseButton({ onClose }: { onClose: () => void }) {
  const { t } = useI18n();
  return (
    <button type="button" className="icon-btn icon-btn--sm" onClick={onClose} aria-label={t('task.back')} title={t('task.back')}>
      <Icon name="x" size={14} />
    </button>
  );
}

/** Task history with filters, and the full story of one task: plan, result and a replayable timeline. */
export function TasksScreen({ selected, onSelect }: { selected: string | null; onSelect: (id: string | null) => void }) {
  const { t } = useI18n();
  const [filter, setFilter] = useState<Filter>('all');
  const [query, setQuery] = useState('');
  const search = useDebounced(query.trim(), 200);
  const active = useAppState((s) => s.tasks.active);
  const recent = useAppState((s) => s.tasks.recent);
  const now = useNow(30000);
  const statusParam: TaskStatus | '' = filter === 'all' || filter === 'active' ? '' : filter;
  const lastFinished = recent[0] ? `${recent[0].id}:${recent[0].updated_at}` : '';
  const history = useResource((api) => api.getTasks({ status: statusParam, limit: 200 }), [statusParam, lastFinished]);
  const tasks = useMemo(() => {
    const merged = mergeTaskLists(history.data ?? [], [...active, ...recent]);
    return merged.filter((task) => matches(task, filter, search));
  }, [history.data, active, recent, filter, search]);

  return (
    <div className="screen screen--tasks" data-testid="tasks-screen">
      <ScreenHeader title={t('tasksScreen.title')} subtitle={t('tasksScreen.subtitle')} icon="tasks" />
      <div className={`tasks-layout ${selected ? 'has-detail' : ''}`}>
        <Panel
          className="tasks-list"
          title={t('nav.tasks')}
          icon="list"
          meta={<span className="num">{tasks.length}</span>}
          flush
          testId="tasks-list"
        >
          <div className="tasks-list__tools">
            <label className="search">
              <Icon name="search" size={14} />
              <input className="input" type="search" value={query} placeholder={t('tasksScreen.search')} aria-label={t('tasksScreen.search')} onChange={(e) => setQuery(e.target.value)} data-testid="tasks-search" />
            </label>
            <Segmented<Filter>
              value={filter}
              onChange={setFilter}
              label={t('tasksScreen.search')}
              options={FILTERS.map((f) => ({ value: f, label: t(`tasksScreen.filter.${f}` as MessageKey) }))}
              testId="tasks-filter"
            />
          </div>
          {history.error && !tasks.length ? (
            <div className="panel-pad">
              <ErrorBox message={history.error} onRetry={history.reload} retryLabel={t('common.retry')} />
            </div>
          ) : history.loading && !tasks.length ? (
            <div className="panel-pad">
              <Loading label={t('common.loading')} rows={6} />
            </div>
          ) : tasks.length ? (
            <ul className="tasks-list__items">
              {tasks.map((task) => (
                <TaskRow key={task.id} task={task} active={task.id === selected} now={now} onSelect={() => onSelect(task.id)} />
              ))}
            </ul>
          ) : (
            <EmptyState icon="tasks" title={t('tasksScreen.empty')} hint={t('tasks.emptyHint')} />
          )}
        </Panel>
        {selected ? (
          <TaskDetailView key={selected} id={selected} onClose={() => onSelect(null)} />
        ) : (
          <div className="tasks-placeholder">
            <EmptyState icon="tasks" title={t('tasksScreen.select')} />
          </div>
        )}
      </div>
    </div>
  );
}
