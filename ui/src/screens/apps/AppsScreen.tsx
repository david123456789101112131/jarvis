import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { apps, type AppsStatus, type AppsTask, type OpenWindow } from '../../api/apps';
import type { Job } from '../../api/work';
import { BidiText } from '../../components/BidiText';
import { Icon } from '../../components/Icon';
import { ScreenHeader } from '../../components/shell/Shell';
import { EmptyState, ErrorBox, Loading } from '../../components/ui';
import { JobCard, Unavailable } from '../../components/work/JobCard';
import { useI18n } from '../../i18n';
import { useDebounced } from '../../lib/hooks';
import { useApi, useAppState, useResource, useRunner, useServices, useStore, useToast } from '../../state/context';
import { InstalledPanel, WindowsPanel } from './AppLists';
import { LookPanel } from './LookPanel';
import { TurnOn, type Gates } from './parts';
import { TaskPanel } from './TaskPanel';
import { WaysPanel } from './WaysPanel';

const SCREEN_KINDS = new Set(['apps.look', 'apps.suggest', 'apps.step']);

/** The "Apps & tools" area. */
export function AppsScreen() {
  const { t } = useI18n();
  const { boot } = useServices();
  if (boot.mock)
    return (
      <div className="area" data-testid="apps-screen">
        <ScreenHeader title={t('apps.title')} subtitle={t('apps.subtitle')} icon="grid" />
        <Unavailable reason={t('unavailable.simulation')} testId="apps-simulation" />
      </div>
    );
  return <AppsLive />;
}

/**
 * The screen on the real core. Every action is a job that goes through JARVIS's permission rules and asks for
 * approval in the usual window; the screen only starts it and shows it live. The apps task (the goal, the
 * corrections, the suggested and done steps) lives in the core, in memory; this keeps its id.
 */
function AppsLive() {
  const { t, lang } = useI18n();
  const api = useApi();
  const store = useStore();
  const toast = useToast();
  const { run, isPending } = useRunner(t('job.actionFailed'));
  const status = useResource((a) => apps.status(a, lang), [lang]);
  const [query, setQuery] = useState('');
  const q = useDebounced(query.trim(), 250);
  const listing = useResource((a) => apps.list(a, q, lang), [q, lang]);
  const jobs = useAppState((s) => s.jobs);
  const killed = useAppState((s) => s.killSwitch.global || 'system' in (s.killSwitch.agents ?? {}));
  const mine = useMemo(() => jobs.filter((j) => j.area === 'apps'), [jobs]);

  const [taskId, setTaskId] = useState<string | null>(null);
  const [task, setTask] = useState<AppsTask | null>(null);
  const [target, setTarget] = useState<OpenWindow | null>(null);
  const [goal, setGoal] = useState('');
  const [dismissed, setDismissed] = useState<ReadonlySet<string>>(() => new Set());

  // The task the core still holds (after a reload of the window): adopted once, on the first status.
  const adopted = useRef(false);
  useEffect(() => {
    const data = status.data;
    if (!data) return;
    if (!adopted.current) {
      adopted.current = true;
      if (data.task) {
        setTaskId(data.task.id);
        setTask(data.task);
        setGoal((g) => g || data.task?.goal || '');
      }
    } else if (data.task && data.task.id === taskId) setTask(data.task);
  }, [status.data, taskId]);

  // A screen action ended: the task's done steps and "is the computer busy" may have changed.
  const ended = useMemo(() => mine.filter((j) => SCREEN_KINDS.has(j.kind) && j.status !== 'running').length, [mine]);
  const lastEnded = useRef(ended);
  const reloadStatus = status.reload;
  useEffect(() => {
    if (ended !== lastEnded.current) {
      lastEnded.current = ended;
      reloadStatus();
    }
  }, [ended, reloadStatus]);

  const ofTask = (j: Job) => taskId !== null && j.params?.task_id === taskId;
  const lookJob = mine.find((j) => j.kind === 'apps.look' && ofTask(j));
  const suggestion = mine.find((j) => j.kind === 'apps.suggest' && ofTask(j));
  const step = suggestion ? mine.find((j) => j.kind === 'apps.step' && j.params?.suggestion === suggestion.id) : undefined;
  const screenRunning = mine.some((j) => SCREEN_KINDS.has(j.kind) && j.status === 'running');

  const gates = useMemo(() => gatesOf(status.data, { killed, inUse: screenRunning, t }), [status.data, killed, screenRunning, t]);

  /** A job the core started: shown at once (unless its live event already came), and its task kept. */
  const adopt = useCallback(
    (job: Job | undefined) => {
      if (!job) return;
      if (!store.getState().jobs.some((j) => j.id === job.id)) store.dispatch({ type: 'event', at: Date.now(), event: { type: 'job', data: job } });
      const id = job.params?.task_id;
      if (typeof id === 'string' && id) setTaskId(id);
    },
    [store],
  );

  const look = (win: OpenWindow | null) => void run('look', () => apps.look(api, taskId, win?.window ?? '', lang)).then(adopt);
  const suggest = () => void run('suggest', () => apps.suggest(api, goal.trim(), taskId, target?.window ?? '', lang)).then(adopt);
  const doStep = (suggestionId: string) => void run('step', () => apps.step(api, suggestionId, lang)).then(adopt);
  const open = (name: string) =>
    void run(`open:${name}`, () => apps.open(api, name, lang)).then((job) => {
      adopt(job);
      if (job) toast('info', t('job.received'), name);
    });
  const correct = async (text: string) => {
    const r = await run('correct', () => apps.correct(api, text, taskId, lang));
    if (!r) return false;
    setTaskId(r.task.id);
    setTask(r.task);
    toast('success', t('apps.correct.saved'));
    return true;
  };
  const endTask = () => {
    if (!taskId) return;
    void run('end', () => apps.endTask(api, taskId)).then((r) => {
      if (!r) return;
      setTaskId(null);
      setTask(null);
      setDismissed(new Set());
      toast('info', t('apps.task.ended'));
    });
  };
  const turnOn = () =>
    void run('turn-on', () => api.patchSettings({ computer: { enabled: true } })).then((r) => {
      if (!r) return;
      status.reload();
      listing.reload();
      toast('success', t('apps.turnedOn'));
    });
  const rescan = () =>
    void run('rescan', () => apps.list(api, q, lang, true)).then((r) => {
      if (r) listing.setData(() => r);
    });
  const refresh = () => {
    status.reload();
    listing.reload();
  };

  const data = status.data;
  const corrections = task?.id === taskId ? task?.corrections ?? [] : [];
  const done = task?.id === taskId ? task?.done ?? [] : [];
  const canCorrect = taskId !== null && (corrections.length > 0 || mine.some((j) => ofTask(j) && j.status === 'done'));
  return (
    <div className="area apps" data-testid="apps-screen">
      <ScreenHeader
        title={t('apps.title')}
        subtitle={t('apps.subtitle')}
        icon="grid"
        actions={
          <button type="button" className="btn btn--sm btn--ghost" onClick={refresh} disabled={status.loading} data-testid="apps-refresh">
            <Icon name="restart" size={13} />
            {t('apps.refresh')}
          </button>
        }
      />
      {data ? (
        <p className="apps-note" data-testid="apps-note">
          <Icon name="lock" size={14} />
          <BidiText text={data.note} />
        </p>
      ) : null}
      {!data && status.error ? <ErrorBox message={`${t('apps.loadFailed')}: ${status.error}`} onRetry={status.reload} retryLabel={t('job.retry')} /> : null}
      {!data && !status.error ? <Loading label={t('apps.title')} /> : null}
      {data ? (
        <div className="apps__cols">
          <div className="area__stack apps__main">
            {!data.computer_enabled ? (
              <Unavailable title={t('apps.off.title')} reason={gates.screen ?? ''} needs={gates.ways.uia?.needs} testId="apps-off">
                <TurnOn onClick={turnOn} pending={isPending('turn-on')} />
              </Unavailable>
            ) : null}
            <LookPanel
              gates={gates}
              target={target}
              onClearTarget={() => setTarget(null)}
              job={lookJob}
              onLook={() => look(target)}
              pending={isPending('look')}
              corrections={corrections}
              canCorrect={canCorrect}
              onCorrect={correct}
              correcting={isPending('correct')}
            />
            <TaskPanel
              gates={gates}
              goal={goal}
              onGoal={setGoal}
              onSuggest={suggest}
              pending={isPending('suggest')}
              suggestion={suggestion}
              step={step}
              dismissed={suggestion ? dismissed.has(suggestion.id) : false}
              onDismiss={() => suggestion && setDismissed((prev) => new Set(prev).add(suggestion.id))}
              onDo={doStep}
              doing={isPending('step')}
              done={done}
              taskId={taskId}
              onEnd={endTask}
              ending={isPending('end')}
            />
            <section className="apps-jobs" aria-label={t('job.recent')} data-testid="apps-jobs">
              <h2 className="apps-sub">{t('job.recent')}</h2>
              <div className="job-list">{mine.length ? mine.map((job) => <JobCard key={job.id} job={job} />) : <EmptyState icon="grid" title={t('job.none')} />}</div>
            </section>
          </div>
          <div className="area__stack apps__side">
            <WaysPanel status={data} />
            <WindowsPanel
              listing={listing}
              target={target}
              gates={gates}
              onChoose={setTarget}
              onLook={(w) => {
                setTarget(w);
                look(w);
              }}
            />
            <InstalledPanel listing={listing} query={query} onQuery={setQuery} gates={gates} onOpen={open} opening={(name) => isPending(`open:${name}`)} onRescan={rescan} rescanning={isPending('rescan')} />
          </div>
        </div>
      ) : null}
    </div>
  );
}

/** Why each kind of action cannot run right now, from the core's status and what this screen is running. */
export function gatesOf(status: AppsStatus | undefined, { killed, inUse, t }: { killed: boolean; inUse: boolean; t: (key: 'apps.killed' | 'apps.busy' | 'apps.inUse' | 'apps.ways.unavailable') => string }): Gates {
  const ways = Object.fromEntries((status?.ways ?? []).map((w) => [w.key, w]));
  const why = (key: string) => {
    const way = ways[key];
    return way === undefined || way.available ? null : way.reason || t('apps.ways.unavailable');
  };
  const busy = killed ? t('apps.killed') : status?.busy ? t('apps.busy') : inUse ? t('apps.inUse') : null;
  return { ways, screen: why('uia'), model: why('vision'), launch: why('launch'), busy, computerOn: status?.computer_enabled ?? true };
}
