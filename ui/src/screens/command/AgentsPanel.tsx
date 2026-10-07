import type { AgentView } from '../../api/types';
import { useI18n, type MessageKey } from '../../i18n';
import { formatRelative } from '../../lib/format';
import type { ScreenId } from '../../lib/router';
import { useAppState, useNow } from '../../state/context';
import { useAgentActions } from '../../state/hooks';
import { Icon } from '../../components/Icon';
import { AGENT_TONE, type Tone } from '../../components/tones';
import { AgentBadge, EmptyState, Loading, Panel, ProgressBar, StatusChip } from '../../components/ui';

export function agentStatusView(agent: AgentView, t: (key: MessageKey) => string): { status: string; tone: Tone; label: string } {
  if (agent.killed) return { status: 'killed', tone: 'danger', label: t('agents.blocked') };
  if (!agent.enabled) return { status: 'disabled', tone: 'muted', label: t('agents.disabled') };
  return { status: agent.status, tone: AGENT_TONE[agent.status], label: t(`agentStatus.${agent.status}` as MessageKey) };
}

function AgentMiniCard({ agent, now, onOpenTask }: { agent: AgentView; now: number; onOpenTask: (id: string) => void }) {
  const { t, lang, agentName } = useI18n();
  const { act, isPending } = useAgentActions();
  const view = agentStatusView(agent, t);
  const busy = agent.status === 'working' || agent.status === 'thinking';
  const control = agent.killed
    ? { action: 'release' as const, icon: 'power', label: t('agents.release') }
    : agent.enabled
      ? { action: 'stop' as const, icon: 'stop', label: t('agents.stop') }
      : { action: 'start' as const, icon: 'play', label: t('agents.start') };
  return (
    <li className="agent-mini" data-status={view.status} data-testid={`agent-card-${agent.id}`}>
      <AgentBadge agent={agent.id} number={agent.number} />
      <div className="agent-mini__main">
        <div className="agent-mini__row">
          <span className="agent-mini__name truncate">{agentName(agent)}</span>
          <StatusChip status={view.status} tone={view.tone} label={view.label} />
        </div>
        {agent.current ? (
          <button type="button" className="agent-mini__task truncate bidi" onClick={() => onOpenTask(agent.current!.task_id)} title={agent.current.title}>
            {agent.current.title}
          </button>
        ) : (
          <span className="agent-mini__task agent-mini__task--none truncate">{agent.queue > 0 ? t('agents.queue', { n: agent.queue }) : t('agents.noTask')}</span>
        )}
        <span className="agent-mini__last truncate" title={agent.last_action ?? undefined}>
          <Icon name="history" size={11} />
          <span className="truncate bidi">{agent.last_action ?? '—'}</span>
          {agent.last_action_at ? <span className="agent-mini__ago">· {formatRelative(agent.last_action_at, lang, now)}</span> : null}
        </span>
      </div>
      <div className="agent-mini__side">
        {busy ? <span className="agent-mini__pct num">{Math.round(agent.progress * 100)}%</span> : null}
        <button
          type="button"
          className="icon-btn icon-btn--sm icon-btn--outline"
          onClick={() => void act(agent.id, control.action)}
          disabled={isPending(`${agent.id}:${control.action}`)}
          aria-label={`${control.label} — ${agentName(agent)}`}
          title={control.label}
          data-testid={`agent-${control.action}-${agent.id}`}
        >
          <Icon name={control.icon} size={13} />
        </button>
      </div>
      {busy ? (
        <div className="agent-mini__progress">
          <ProgressBar value={agent.progress} tone={view.tone} variant="thin" label={agentName(agent)} />
        </div>
      ) : null}
    </li>
  );
}

export function AgentsPanel({ className, onNavigate }: { className?: string; onNavigate: (screen: ScreenId, param?: string | null) => void }) {
  const { t } = useI18n();
  const agents = useAppState((s) => s.agents);
  const hydrated = useAppState((s) => s.hydrated);
  const now = useNow(15000);
  const busy = agents.filter((a) => a.status === 'working' || a.status === 'thinking').length;
  return (
    <Panel
      className={className}
      title={t('agents.title')}
      icon="agents"
      meta={<span className="num">{t('agents.busy', { n: busy })}</span>}
      actions={
        <button type="button" className="icon-btn icon-btn--sm" onClick={() => onNavigate('agents')} aria-label={t('nav.agents')} title={t('nav.agents')}>
          <Icon name="external" size={14} />
        </button>
      }
      testId="panel-agents"
      bodyClassName="agents-panel__body"
    >
      {!hydrated ? (
        <Loading label={t('common.loading')} rows={5} />
      ) : agents.length ? (
        <ul className="agent-list">
          {agents.map((agent) => (
            <AgentMiniCard key={agent.id} agent={agent} now={now} onOpenTask={(id) => onNavigate('tasks', id)} />
          ))}
        </ul>
      ) : (
        <EmptyState icon="agents" title={t('agents.waiting')} />
      )}
    </Panel>
  );
}
