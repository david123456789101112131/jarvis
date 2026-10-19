import { useCallback, useEffect, useMemo, useState } from 'react';
import { ApiError } from '../../api/client';
import {
  dev,
  type DevChange,
  type DevChangeItem,
  type DevReport,
  type DevSession,
  type DevStatus,
  type EngineMode,
  type InspectResult,
  type TestsResult,
} from '../../api/dev';
import type { Job } from '../../api/work';
import { ScreenHeader } from '../../components/shell/Shell';
import { BidiText } from '../../components/BidiText';
import { Panel } from '../../components/ui';
import { JobCard, Unavailable } from '../../components/work/JobCard';
import { useI18n, type MessageKey } from '../../i18n';
import { formatRelative } from '../../lib/format';
import { errorMessage, useApi, useAppState, useRunner, useStore, useToast } from '../../state/context';
import { ChangePanel } from './ChangePanel';
import { DonePanel, Flow, type FlowState, type FlowStep } from './Flow';
import { ProjectPanel } from './ProjectPanel';
import { RequestPanel, type PickedFile } from './RequestPanel';
import { TestsPanel } from './TestsPanel';

type Tracked = 'open' | 'files' | 'propose' | 'apply' | 'tests';

function result<T>(job: Job | null): T | null {
  return job && job.status === 'done' && job.result ? (job.result as unknown as T) : null;
}

/**
 * The "Code & development" area: pick a project → describe what is needed → find the files → plan with the AI
 * the user presses → review the change (edit, leave out, discard) → apply (with undo) → run the tests → fix.
 * Every step is a job card that says what JARVIS understood, what it does, whether it is real, what leaves the
 * computer and what it costs, with Stop and Try again.
 */
export function CodeScreen() {
  const { t, lang } = useI18n();
  const api = useApi();
  const store = useStore();
  const toast = useToast();
  const { run, isPending } = useRunner(t('job.actionFailed'));

  const [status, setStatus] = useState<DevStatus | null>(null);
  const [statusError, setStatusError] = useState<ApiError | null>(null);
  const [session, setSession] = useState<DevSession | null>(null);
  const [report, setReport] = useState<DevReport | null>(null);
  const [git, setGit] = useState<InspectResult['git']>(null);
  const [listing, setListing] = useState<string[]>([]);
  const [files, setFiles] = useState<PickedFile[]>([]);
  const [request, setRequest] = useState('');
  const [errorText, setErrorText] = useState('');
  const [mode, setMode] = useState<EngineMode>('auto');
  const [engineId, setEngineId] = useState<string | undefined>(undefined);
  const [change, setChange] = useState<DevChange | null>(null);
  const [history, setHistory] = useState<DevChangeItem[]>([]);
  const [tracked, setTracked] = useState<Partial<Record<Tracked, string>>>({});
  const [applyFor, setApplyFor] = useState<string | null>(null);

  const jobs = useAppState((s) => s.jobs);
  const job = useCallback((key: Tracked) => (tracked[key] ? jobs.find((j) => j.id === tracked[key]) ?? null : null), [jobs, tracked]);
  const openJob = job('open');
  const filesJob = job('files');
  const proposeJob = job('propose');
  const applyJob = job('apply');
  const testsJob = job('tests');

  const engines = status?.engines ?? [];
  const engine = engines.find((e) => e.provider === engineId) ?? engines[0];
  const engineName = engine?.name ?? 'AI';
  const sim = statusError?.status === 501;

  /** Show a job the moment it starts (its WebSocket events update it afterwards). */
  const track = useCallback(
    (key: Tracked, started: Job | undefined) => {
      if (!started) return;
      if (!store.getState().jobs.some((j) => j.id === started.id)) store.dispatch({ type: 'event', at: Date.now(), event: { type: 'job', data: started } });
      setTracked((prev) => ({ ...prev, [key]: started.id }));
    },
    [store],
  );

  const loadHistory = useCallback(
    async (id: string) => {
      try {
        setHistory(await dev.changes(api, id));
      } catch {
        setHistory([]);
      }
    },
    [api],
  );

  const loadChange = useCallback(
    async (id: string) => {
      try {
        setChange(await dev.change(api, id));
      } catch (err) {
        toast('error', t('job.actionFailed'), errorMessage(err));
      }
    },
    [api, toast, t],
  );

  // ---------------------------------------------------------------- the status, and a project still open
  useEffect(() => {
    let alive = true;
    dev
      .status(api)
      .then((s) => {
        if (!alive) return;
        setStatus(s);
        const last = s.sessions[s.sessions.length - 1];
        if (last) {
          setSession(last);
          setReport(last.report);
          void loadHistory(last.id);
          if (last.last_change) void loadChange(last.last_change);
        }
      })
      .catch((err: unknown) => alive && setStatusError(err instanceof ApiError ? err : new ApiError(0, 'error', errorMessage(err))));
    return () => {
      alive = false;
    };
  }, [api, loadChange, loadHistory]);

  // ---------------------------------------------------------------- results of the jobs this screen started
  const openDone = result<InspectResult>(openJob);
  useEffect(() => {
    if (!openDone) return;
    setReport(openDone.report);
    setGit(openDone.git);
    setListing(openDone.listing);
  }, [openDone]);

  const found = result<InspectResult>(filesJob);
  useEffect(() => {
    if (!found) return;
    setListing(found.listing);
    setFiles(found.ranked.map((r) => ({ ...r, picked: true })));
  }, [found]);

  const sessionId = session?.id ?? null;
  const proposed = result<{ change_id: string }>(proposeJob);
  useEffect(() => {
    if (!proposed || !sessionId) return;
    void loadChange(proposed.change_id);
    void loadHistory(sessionId);
  }, [proposed, sessionId, loadChange, loadHistory]);

  // When an apply or undo finishes (well or not), show the change as it is now.
  const applyFinished = applyJob && applyJob.status !== 'running' ? applyJob.id : null;
  useEffect(() => {
    if (!applyFinished || !applyFor || !sessionId) return;
    void loadChange(applyFor);
    void loadHistory(sessionId);
  }, [applyFinished, applyFor, sessionId, loadChange, loadHistory]);

  const tests = result<TestsResult>(testsJob);

  // ---------------------------------------------------------------- actions
  const pick = (path: string) => {
    void run('open', async () => {
      const opened = await dev.open(api, path, lang);
      setTracked({});
      setSession(opened.session);
      setReport(opened.session.report);
      setChange(null);
      setFiles([]);
      track('open', opened.job);
      void loadHistory(opened.session.id);
    });
  };

  const close = () =>
    session &&
    void run('close', async () => {
      await dev.close(api, session.id);
      setSession(null);
      setReport(null);
      setChange(null);
      setFiles([]);
      setListing([]);
      setHistory([]);
      setTracked({});
      toast('info', t('code.closed'));
    });

  const refresh = () => session && void run('refresh', async () => track('open', await dev.inspect(api, session.id, { refresh: true, lang })));

  const allow = (on: boolean) =>
    session &&
    void run('allow', async () => {
      const next = await dev.allowChanges(api, session.id, on, lang);
      setSession(next);
      setChange((c) => (c ? { ...c, writable: next.writable } : c));
    });

  const find = () => session && void run('files', async () => track('files', await dev.inspect(api, session.id, { request, error_text: errorText, lang })));

  const plan = (extra?: { request?: string; parent_id?: string }) =>
    session &&
    void run('propose', async () => {
      const ask = extra?.request ?? request;
      const picked = files.filter((f) => f.picked).map((f) => f.path);
      track('propose', await dev.propose(api, session.id, { request: ask, files: picked, mode, error_text: errorText, parent_id: extra?.parent_id, engine: engine?.provider, lang }));
    });

  const toggleFile = (path: string, picked: boolean) => setFiles((prev) => prev.map((f) => (f.path === path ? { ...f, picked } : f)));
  const addFile = (path: string) => {
    if (listing.length && !listing.includes(path)) return false;
    setFiles((prev) => (prev.some((f) => f.path === path) ? prev.map((f) => (f.path === path ? { ...f, picked: true } : f)) : [...prev, { path, score: 0, size: 0, reasons: [], lines: [], picked: true }]));
    return true;
  };

  const editFile = async (path: string, body: { content?: string; include?: boolean }) => {
    if (!change) return false;
    const next = await run('edit', () => dev.editFile(api, change.id, { path, ...body, lang }));
    if (next) setChange(next);
    return Boolean(next);
  };

  const apply = (undo = false) =>
    change &&
    void run('apply', async () => {
      setApplyFor(change.id);
      track('apply', await (undo ? dev.undo(api, change.id, lang) : dev.apply(api, change.id, lang)));
    });

  const discard = () =>
    change &&
    void run('discard', async () => {
      setChange(await dev.discard(api, change.id, lang));
      toast('info', t('code.discarded'));
      if (session) void loadHistory(session.id);
    });

  const runTests = () => session && void run('tests', async () => track('tests', await dev.tests(api, session.id, lang)));

  const fix = () =>
    session &&
    testsJob &&
    void run('propose', async () => track('propose', await dev.fix(api, session.id, { tests_job_id: testsJob.id, mode, engine: engine?.provider, lang })));

  // ---------------------------------------------------------------- the flow
  const running = (j: Job | null) => j?.status === 'running';
  const failed = (j: Job | null) => j?.status === 'failed';
  const applied = change ? change.files.some((f) => f.status === 'applied') : false;
  const states: Record<FlowStep, FlowState> = useMemo(
    () => ({
      request: request.trim() || change ? 'done' : session ? 'active' : 'todo',
      project: report ? 'done' : running(openJob) ? 'active' : failed(openJob) ? 'failed' : 'todo',
      plan: running(proposeJob) ? 'active' : failed(proposeJob) ? 'failed' : change ? 'done' : 'todo',
      change: applied ? 'done' : change && change.status === 'proposed' ? 'active' : failed(applyJob) ? 'failed' : 'todo',
      tests: running(testsJob) ? 'active' : tests ? (tests.ok ? 'done' : 'failed') : 'todo',
      summary: applied && tests ? 'done' : applied ? 'active' : 'todo',
    }),
    [request, change, session, report, openJob, proposeJob, applyJob, testsJob, tests, applied],
  );

  const current = useMemo(() => {
    const mine = jobs.filter((j) => j.area === 'code' && (!session || j.params.session === session.id || j.params.change === change?.id));
    return mine.slice(0, 4);
  }, [jobs, session, change]);

  const busy = isPending('open') || isPending('close') || isPending('allow');
  const enabled = Boolean(session) && !busy;

  if (sim || (statusError && !status)) {
    return (
      <div className="area" data-testid="code-screen">
        <ScreenHeader title={t('code.title')} subtitle={t('code.subtitle')} icon="code" />
        <Unavailable reason={sim ? t('unavailable.simulation') : statusError?.message ?? ''} testId="code-unavailable" />
      </div>
    );
  }

  return (
    <div className="area" data-testid="code-screen">
      <ScreenHeader title={t('code.title')} subtitle={t('code.subtitle')} icon="code" />
      <Flow states={states} />
      <div className="area__cols">
        <div className="area__stack">
          <ProjectPanel session={session} report={report} git={git} opening={isPending('open') || running(openJob)} onPick={pick} onAllow={allow} onClose={close} onRefresh={refresh} busy={busy} />
          <RequestPanel
            enabled={enabled}
            request={request}
            onRequest={setRequest}
            errorText={errorText}
            onErrorText={setErrorText}
            files={files}
            onToggleFile={toggleFile}
            onAddFile={addFile}
            listing={listing}
            finding={isPending('files') || running(filesJob)}
            onFind={find}
            engines={engines}
            engine={engine}
            onEngine={setEngineId}
            mode={mode}
            onMode={setMode}
            pricesChecked={status?.prices_checked ?? ''}
            needs={(status?.needs ?? []).map((n) => (lang === 'he' ? n.he : n.en))}
            planning={isPending('propose') || running(proposeJob)}
            onPlan={() => plan()}
          />
        </div>
        <div className="area__stack">
          {current.length ? (
            <div className="job-list" data-testid="code-jobs">
              {current.map((j, i) => (
                <JobCard key={j.id} job={j} compact={i > 0} />
              ))}
            </div>
          ) : null}
          {change ? (
            <ChangePanel
              change={change}
              engineName={engineName}
              busy={isPending('edit') || isPending('discard') || isPending('propose')}
              applying={isPending('apply') || running(applyJob)}
              onInclude={(path, include) => void editFile(path, { include })}
              onSave={(path, content) => editFile(path, { content })}
              onApply={() => apply(false)}
              onUndo={() => apply(true)}
              onDiscard={discard}
              onRevise={(text) => plan({ request: text, parent_id: change.id })}
            />
          ) : null}
          {session ? (
            <TestsPanel
              command={report?.commands.test ?? ''}
              enabled={enabled}
              running={isPending('tests') || running(testsJob)}
              result={tests}
              engineName={engineName}
              canFix={Boolean(engine)}
              onRun={runTests}
              onFix={fix}
            />
          ) : null}
          {change && applied ? <DonePanel change={change} tests={tests} /> : null}
          {history.length > 1 ? (
            <Panel title={t('code.history')} icon="history" testId="code-history">
              <ul className="code-history">
                {history.map((h) => (
                  <li key={h.id} data-current={h.id === change?.id || undefined}>
                    <span className="code-history__status">{t(`code.status.${h.status}` as MessageKey)}</span>
                    <BidiText text={h.summary || h.request} />
                    <span className="field__hint">{formatRelative(h.created_at, lang)}</span>
                    {h.id !== change?.id ? (
                      <button type="button" className="btn btn--sm btn--ghost" onClick={() => void loadChange(h.id)} data-testid={`code-history-${h.id}`}>
                        {t('code.historyOpen')}
                      </button>
                    ) : null}
                  </li>
                ))}
              </ul>
            </Panel>
          ) : null}
        </div>
      </div>
    </div>
  );
}
