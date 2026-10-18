import { useMemo, useState } from 'react';
import { AGENT_IDS, PRIORITIES, type ActivityEntry, type AgentId, type AgentView, type Priority, type ProviderName } from '../api/types';
import { useI18n, type MessageKey } from '../i18n';
import { formatDuration, formatRelative, formatTimeOf, textDir } from '../lib/format';
import type { ScreenId } from '../lib/router';
import { useApi, useAppState, useNow, useResource, useRunner, useToast } from '../state/context';
import { useAgentActions } from '../state/hooks';
import { Icon } from '../components/Icon';
import { ScreenHeader } from '../components/shell/Shell';
import { AgentBadge, Chip, ConfirmButton, EmptyState, Loading, Panel, ProgressBar, StatusChip } from '../components/ui';
import { ActivityLine } from './command/ActivityPanel';
import { agentStatusView } from './command/AgentsPanel';

type Navigate = (screen: ScreenId, param?: string | null) => void;

const ACTIVITY_LIMIT = 150;

function isAgentId(value: string | null): value is AgentId {
  return value !== null && (AGENT_IDS as readonly string[]).includes(value);
}

function isBusy(agent: AgentView): boolean {
  return agent.status === 'working' || agent.status === 'thinking';
}

const PROVIDER_LABELS: Record<ProviderName, string> = {
  anthropic: 'Anthropic (Claude)',
  xai: 'xAI (Grok)',
  openai: 'OpenAI',
  local: '',
};

function AgentModel({ agent }: { agent: AgentView }) {
  const { t } = useI18n();
  const ai = agent.ai;
  const label = (p: ProviderName) => PROVIDER_LABELS[p] || t('settings.local');
  return (
    <section className="agent-section" data-testid="agent-ai">
      <p className="section-label">{t('agents.model')}</p>
      {ai?.provider ? (
        <p className="agent-last">
          <Chip tone="info" dot>
            {label(ai.provider)}
          </Chip>
          {ai.model ? <span className="ltr faint"> {ai.model}</span> : null}
        </p>
      ) : (
        <p className="hint">{t('agents.noModel')}</p>
      )}
      {ai?.when_told ? <p className="hint" data-testid="agent-ai-when-told">{t('agents.whenTold')}</p> : null}
      {ai && ai.preferred !== 'auto' && ai.provider !== ai.preferred ? (
        <p className="hint" data-testid="agent-ai-fallback">{t('agents.modelFallback', { provider: label(ai.preferred) })}</p>
      ) : null}
    </section>
  );
}

function AgentListItem({ agent, active, onSelect }: { agent: AgentView; active: boolean; onSelect: () => void }) {
  const { t, agentName, agentRole } = useI18n();
  const view = agentStatusView(agent, t);
  const busy = isBusy(agent);
  return (
    <li>
      <button type="button" className="agent-card" aria-current={active ? 'true' : undefined} data-status={view.status} onClick={onSelect} data-testid={`agents-item-${agent.id}`}>
        <AgentBadge agent={agent.id} number={agent.number} size="lg" />
        <span className="agent-card__main">
          <span className="agent-card__row">
            <span className="agent-card__name truncate">{agentName(agent)}</span>
            <StatusChip status={view.status} tone={view.tone} label={view.label} />
          </span>
          <span className="agent-card__role truncate">{agentRole(agent)}</span>
          {busy ? (
            <span className="agent-card__progress">
              <ProgressBar value={agent.progress} tone={view.tone} variant="thin" label={agentName(agent)} />
              <span className="num">{Math.round(agent.progress * 100)}%</span>
            </span>
          ) : (
            <span className="agent-card__task truncate bidi">{agent.current?.title ?? (agent.queue > 0 ? t('agents.queue', { n: agent.queue }) : t('agents.noTask'))}</span>
          )}
        </span>
      </button>
    </li>
  );
}

function AgentControls({ agent }: { agent: AgentView }) {
  const { t, agentName } = useI18n();
  const { act, isPending } = useAgentActions();
  const name = agentName(agent);
  if (agent.killed) {
    return (
      <button type="button" className="btn btn--sm btn--ok" onClick={() => void act(agent.id, 'release')} disabled={isPending(`${agent.id}:release`)} data-testid="agent-release">
        <Icon name="power" size={14} />
        {t('agents.release')}
      </button>
    );
  }
  return (
    <>
      {agent.enabled ? (
        <button type="button" className="btn btn--sm" onClick={() => void act(agent.id, 'stop')} disabled={isPending(`${agent.id}:stop`)} data-testid="agent-stop">
          <Icon name="pause" size={14} />
          {t('agents.stop')}
        </button>
      ) : (
        <button type="button" className="btn btn--sm btn--ok" onClick={() => void act(agent.id, 'start')} disabled={isPending(`${agent.id}:start`)} data-testid="agent-start">
          <Icon name="play" size={14} />
          {t('agents.start')}
        </button>
      )}
      <button type="button" className="btn btn--sm" onClick={() => void act(agent.id, 'restart')} disabled={isPending(`${agent.id}:restart`)} data-testid="agent-restart">
        <Icon name="restart" size={14} />
        {t('agents.restart')}
      </button>
      <ConfirmButton className="btn btn--sm btn--danger" confirmLabel={t('common.confirm')} title={t('agents.killConfirm', { name })} onConfirm={() => void act(agent.id, 'kill')} disabled={isPending(`${agent.id}:kill`)} testId="agent-kill">
        <Icon name="octagon" size={14} />
        {t('agents.kill')}
      </ConfirmButton>
    </>
  );
}

function AssignForm({ agent, blocked }: { agent: AgentView; blocked: boolean }) {
  const { t, agentName } = useI18n();
  const api = useApi();
  const toast = useToast();
  const [text, setText] = useState('');
  const [priority, setPriority] = useState<Priority>('normal');
  const { run, isPending } = useRunner(t('common.requestFailed'));
  const name = agentName(agent);
  const submit = () => {
    const instruction = text.trim();
    if (!instruction || blocked) return;
    void run('assign', async () => {
      await api.assignTask(agent.id, instruction, priority);
      setText('');
      toast('success', t('agents.assigned', { name }), instruction);
    });
  };
  return (
    <form
      className="assign"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
      data-testid="agent-assign"
    >
      <textarea
        className="textarea"
        rows={2}
        value={text}
        dir={text.trim() ? textDir(text) : undefined}
        placeholder={t('agents.assignPlaceholder', { name })}
        aria-label={t('agents.assign')}
        disabled={blocked}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
            e.preventDefault();
            submit();
          }
        }}
      />
      <div className="assign__row">
        <label className="assign__priority">
          <span className="field__label">{t('task.priority')}</span>
          <select className="select select--sm" value={priority} onChange={(e) => setPriority(e.target.value as Priority)} disabled={blocked}>
            {PRIORITIES.map((p) => (
              <option key={p} value={p}>
                {t(`priority.${p}` as MessageKey)}
              </option>
            ))}
          </select>
        </label>
        {blocked ? <span className="hint">{t('agents.assignBlocked')}</span> : <span className="hint">{t('agents.assignHint')}</span>}
        <button type="submit" className="btn btn--primary btn--sm" disabled={blocked || !text.trim() || isPending('assign')} data-testid="agent-assign-send">
          {isPending('assign') ? <span className="spinner spinner--xs" /> : <Icon name="send" size={13} />}
          {t('agents.assignSend')}
        </button>
      </div>
    </form>
  );
}

function AgentActivity({ agent }: { agent: AgentView }) {
  const { t } = useI18n();
  const live = useAppState((s) => s.activity);
  const history = useResource((api) => api.getAgent(agent.id), [agent.id]);
  const entries = useMemo(() => {
    const byId = new Map<string, ActivityEntry>();
    for (const e of history.data?.activity ?? []) byId.set(e.id, e);
    for (const e of live) if (e.agent_id === agent.id) byId.set(e.id, e);
    return [...byId.values()].sort((a, b) => (a.ts < b.ts ? 1 : a.ts > b.ts ? -1 : 0)).slice(0, ACTIVITY_LIMIT);
  }, [history.data, live, agent.id]);
  return (
    <Panel className="agent-activity" title={t('agents.activity')} icon="history" meta={entries.length ? <span className="num">{entries.length}</span> : null} flush testId="agent-activity">
      {history.loading && !entries.length ? (
        <div className="panel-pad">
          <Loading label={t('common.loading')} rows={4} />
        </div>
      ) : entries.length ? (
        <ol className="log log--static">
          {entries.map((entry) => (
            <ActivityLine key={entry.id} entry={entry} />
          ))}
        </ol>
      ) : (
        <EmptyState icon="history" title={t('agents.noActivity')} />
      )}
    </Panel>
  );
}

function AgentDetail({ agent, onNavigate }: { agent: AgentView; onNavigate: Navigate }) {
  const { t, lang, agentName, agentRole } = useI18n();
  const globalKill = useAppState((s) => s.killSwitch.global);
  const now = useNow(15000);
  const view = agentStatusView(agent, t);
  const busy = isBusy(agent);
  const blocked = agent.killed || globalKill;
  const name = agentName(agent);
  const stats = [
    { key: 'completed', label: t('agents.statCompleted'), value: String(agent.stats.completed), tone: 'ok' },
    { key: 'failed', label: t('agents.statFailed'), value: String(agent.stats.failed), tone: agent.stats.failed ? 'danger' : 'steel' },
    { key: 'avg', label: t('agents.statAvg'), value: formatDuration(agent.stats.avg_duration_s, lang), tone: 'steel' },
    { key: 'queue', label: t('agents.statQueue'), value: String(agent.queue + (agent.running ?? 0)), tone: agent.queue ? 'warn' : 'steel' },
  ];
  return (
    <div className="agents-detail" data-testid={`agent-detail-${agent.id}`}>
      <Panel
        className="agent-main"
        title={t('agents.number', { n: agent.number })}
        icon={agent.id}
        actions={<AgentControls agent={agent} />}
        testId="agent-main"
      >
        <div className="agent-hero" data-status={view.status}>
          <AgentBadge agent={agent.id} number={agent.number} size="lg" />
          <div className="agent-hero__titles">
            <h2 className="agent-hero__name">{name}</h2>
            <p className="agent-hero__role">{agentRole(agent)}</p>
          </div>
          <StatusChip status={view.status} tone={view.tone} label={view.label} size="lg" testId="agent-status" />
        </div>
        {globalKill && !agent.killed ? (
          <p className="notice notice--danger">
            <Icon name="octagon" size={14} />
            {t('agents.globalKill')}
          </p>
        ) : null}
        <dl className="stat-grid">
          {stats.map((s) => (
            <div key={s.key} className="stat" data-tone={s.tone}>
              <dt>{s.label}</dt>
              <dd className="num">{s.value}</dd>
            </div>
          ))}
        </dl>

        <section className="agent-section">
          <p className="section-label">{t('agents.currentTask')}</p>
          {agent.current ? (
            <div className="agent-current">
              <button type="button" className="link-btn" onClick={() => onNavigate('tasks', agent.current!.task_id)} title={agent.current.title}>
                <Icon name="tasks" size={13} />
                <span className="truncate bidi">{agent.current.title}</span>
              </button>
              {busy ? (
                <div className="agent-current__progress">
                  <ProgressBar value={agent.progress} tone={view.tone} label={agent.current.title} />
                  <span className="num">{Math.round(agent.progress * 100)}%</span>
                </div>
              ) : null}
            </div>
          ) : (
            <p className="hint">{agent.queue > 0 ? t('agents.queue', { n: agent.queue }) : t('agents.noTask')}</p>
          )}
        </section>

        <section className="agent-section">
          <p className="section-label">{t('agents.lastAction')}</p>
          {agent.last_action ? (
            <p className="agent-last">
              <span className="bidi">{agent.last_action}</span>
              {agent.last_action_at ? <span className="faint"> · {formatRelative(agent.last_action_at, lang, now)}</span> : null}
            </p>
          ) : (
            <p className="hint">—</p>
          )}
        </section>

        <AgentModel agent={agent} />

        <section className="agent-section">
          <p className="section-label">{t('agents.assign')}</p>
          <AssignForm agent={agent} blocked={blocked} />
        </section>

        <div className="agent-columns">
          <section className="agent-section">
            <p className="section-label">{t('agents.skills')}</p>
            {agent.skills.length ? (
              <ul className="chip-list">
                {agent.skills.map((skill) => (
                  <li key={skill}>
                    <Chip tone="steel" className="chip--mono">
                      {skill}
                    </Chip>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="hint">—</p>
            )}
          </section>
          <section className="agent-section">
            <p className="section-label">{t('agents.permissions')}</p>
            <ul className="chip-list" data-testid="agent-permissions">
              {agent.permissions.map((cap) => (
                <li key={cap}>
                  <Chip tone="accent" className="chip--mono">
                    {cap}
                  </Chip>
                </li>
              ))}
            </ul>
            <button type="button" className="link-btn link-btn--sm" onClick={() => onNavigate('security')}>
              <Icon name="shield" size={12} />
              {t('agents.managePermissions')}
            </button>
          </section>
        </div>

        <section className="agent-section">
          <p className="section-label">{t('agents.errors')}</p>
          {agent.errors.length ? (
            <ul className="error-list">
              {agent.errors
                .slice()
                .reverse()
                .map((err, i) => (
                  <li key={`${err.at}-${i}`}>
                    <span className="num ltr faint">{formatTimeOf(err.at)}</span>
                    <span className="bidi">{err.message}</span>
                  </li>
                ))}
            </ul>
          ) : (
            <p className="hint">{t('agents.noErrors')}</p>
          )}
        </section>
      </Panel>
      <AgentActivity agent={agent} />
    </div>
  );
}

/** The five agents: status, controls, direct assignment, skills, permissions, errors and activity. */
export function AgentsScreen({ selected, onSelect, onNavigate }: { selected: string | null; onSelect: (id: AgentId) => void; onNavigate: Navigate }) {
  const { t } = useI18n();
  const agents = useAppState((s) => s.agents);
  const current = agents.find((a) => a.id === (isAgentId(selected) ? selected : null)) ?? agents[0] ?? null;
  const busy = agents.filter(isBusy).length;
  return (
    <div className="screen screen--agents" data-testid="agents-screen">
      <ScreenHeader title={t('agents.title')} subtitle={t('agents.subtitle')} icon="agents">
        <Chip tone={busy ? 'work' : 'steel'} dot pulse={busy > 0}>
          {t('agents.busy', { n: busy })}
        </Chip>
      </ScreenHeader>
      {agents.length ? (
        <div className="agents-layout">
          <nav className="agents-nav" aria-label={t('agents.title')}>
            <ul className="agents-nav__list">
              {agents.map((agent) => (
                <AgentListItem key={agent.id} agent={agent} active={agent.id === current?.id} onSelect={() => onSelect(agent.id)} />
              ))}
            </ul>
          </nav>
          {current ? <AgentDetail key={current.id} agent={current} onNavigate={onNavigate} /> : null}
        </div>
      ) : (
        <EmptyState icon="agents" title={t('agents.waiting')} />
      )}
    </div>
  );
}
