import { useEffect, useMemo, useState } from 'react';
import { AGENT_IDS, type AgentId, type AgentPermissions, type AuditEntry, type ConfirmationRequest, type Grant } from '../api/types';
import { useI18n, type MessageKey } from '../i18n';
import { formatDateTime, formatTimeOf, textDir } from '../lib/format';
import type { ScreenId } from '../lib/router';
import { useApi, useAppState, useDispatch, useNow, useResource, useRunner, useToast } from '../state/context';
import { secondsLeft } from '../components/ConfirmModal';
import { Icon } from '../components/Icon';
import { ScreenHeader } from '../components/shell/Shell';
import { AGENT_NUMBERS } from '../components/tasks';
import { DECISION_TONE, OUTCOME_TONE, riskTone } from '../components/tones';
import { AgentBadge, Chip, ConfirmButton, EmptyState, ErrorBox, Loading, Panel } from '../components/ui';
import { KillSwitchControl } from './command/FilesPanel';
import { BidiText } from '../components/BidiText';

type Navigate = (screen: ScreenId, param?: string | null) => void;

const DECISIONS = ['allowed', 'confirmed', 'denied', 'rejected', 'blocked', 'error'] as const;

/** Pending requests, the one that expires first on top. */
function pendingFirst(all: readonly ConfirmationRequest[]): ConfirmationRequest[] {
  return all.filter((c) => c.status === 'pending').sort((a, b) => (a.created_at < b.created_at ? -1 : a.created_at > b.created_at ? 1 : 0));
}

function riskKey(risk: string): MessageKey {
  return `risk.${risk.toLowerCase()}` as MessageKey;
}

function ApprovalRow({ request, now }: { request: ConfirmationRequest; now: number }) {
  const { t, agentName } = useI18n();
  const api = useApi();
  const dispatch = useDispatch();
  const toast = useToast();
  const { run, isPending } = useRunner(t('common.requestFailed'));
  const left = secondsLeft(request, now);
  const answer = (approved: boolean) =>
    void run(approved ? 'approve' : 'deny', async () => {
      await api.answerConfirmation(request.id, approved, false);
      dispatch({ type: 'confirmation.resolved', id: request.id });
      toast(approved ? 'success' : 'info', approved ? t('confirm.approved') : t('confirm.denied'), request.summary);
    });
  return (
    <li className="approval" data-tone={riskTone(request.risk)} data-testid="security-approval">
      <AgentBadge agent={request.agent_id} number={AGENT_NUMBERS[request.agent_id] ?? '•'} />
      <div className="approval__main">
        <p className="approval__summary bidi" dir={textDir(request.summary)}>
          <BidiText text={request.summary} />
        </p>
        <p className="approval__meta">
          <span>{agentName(request.agent_id)}</span>
          <code className="ltr">{request.tool}</code>
          <Chip tone={riskTone(request.risk)}>{t('confirm.risk', { risk: t(riskKey(request.risk)) })}</Chip>
          {left !== null ? <span className="num">{t('confirm.expiresIn', { s: left })}</span> : null}
        </p>
        {request.reasons.length ? (
          <ul className="approval__reasons">
            {request.reasons.map((r, i) => (
              <li key={i} className="bidi">
                <BidiText text={r} />
              </li>
            ))}
          </ul>
        ) : null}
      </div>
      <div className="approval__actions">
        <button type="button" className="btn btn--sm btn--danger" disabled={isPending('deny') || isPending('approve')} onClick={() => answer(false)} data-testid="security-deny">
          <Icon name="x" size={13} />
          {t('confirm.deny')}
        </button>
        <button type="button" className="btn btn--sm btn--ok" disabled={isPending('deny') || isPending('approve')} onClick={() => answer(true)} data-testid="security-approve">
          <Icon name="check" size={13} />
          {t('confirm.approve')}
        </button>
      </div>
    </li>
  );
}

function ApprovalsPanel() {
  const { t } = useI18n();
  const confirmations = useAppState((s) => s.confirmations);
  const list = useMemo(() => pendingFirst(confirmations), [confirmations]);
  const now = useNow(1000, list.length > 0);
  return (
    <Panel className="sec-approvals" title={t('security.pending')} icon="shield" meta={list.length ? <Chip tone="warn">{list.length}</Chip> : null} testId="security-approvals">
      {list.length ? (
        <ul className="approvals">
          {list.map((r) => (
            <ApprovalRow key={r.id} request={r} now={now} />
          ))}
        </ul>
      ) : (
        <EmptyState icon="check-circle" title={t('security.noPending')} hint={t('security.pendingHint')} />
      )}
    </Panel>
  );
}

function KillSwitchesPanel() {
  const { t, agentName } = useI18n();
  const api = useApi();
  const kill = useAppState((s) => s.killSwitch);
  const { run, isPending } = useRunner(t('common.requestFailed'));
  return (
    <Panel className="sec-kill" title={t('security.killSwitches')} icon="octagon" testId="security-kill">
      <div className="kill-global">
        <p className="kill-global__label">{t('security.global')}</p>
        <KillSwitchControl />
      </div>
      <ul className="kill-agents">
        {AGENT_IDS.map((id) => {
          const engaged = id in kill.agents;
          return (
            <li key={id} className="kill-agent" data-engaged={engaged ? 'true' : 'false'} data-testid={`kill-agent-${id}`}>
              <AgentBadge agent={id} number={AGENT_NUMBERS[id]} size="sm" />
              <span className="kill-agent__name truncate">{agentName(id)}</span>
              <Chip tone={engaged ? 'danger' : 'ok'} dot>
                {engaged ? t('security.engaged') : t('security.normal')}
              </Chip>
              {engaged ? (
                <button type="button" className="btn btn--sm btn--ok" disabled={isPending(id)} onClick={() => void run(id, () => api.killSwitch(false, id))}>
                  {t('security.release')}
                </button>
              ) : (
                <ConfirmButton className="btn btn--sm btn--danger" confirmLabel={t('common.confirm')} title={t('agents.killConfirm', { name: agentName(id) })} disabled={isPending(id)} onConfirm={() => void run(id, () => api.killSwitch(true, id, t('kill.reason')))}>
                  {t('security.engage')}
                </ConfirmButton>
              )}
            </li>
          );
        })}
      </ul>
    </Panel>
  );
}

function GrantRow({ grant, onRevoke, busy }: { grant: Grant; onRevoke: () => void; busy: boolean }) {
  const { t, lang } = useI18n();
  return (
    <li className="grant" data-testid="security-grant">
      <Chip tone="accent" className="chip--mono">
        {grant.capability}
      </Chip>
      <span className="grant__info">
        {grant.roots.length ? <span className="ltr mono truncate">{grant.roots.join(', ')}</span> : null}
        <span className="faint">
          {grant.task_id ? <span className="mono ltr">{grant.task_id}</span> : t('security.anyTask')}
          {' · '}
          {grant.expires_at ? t('security.expires', { t: formatDateTime(grant.expires_at, lang) }) : t('security.noExpiry')}
          {grant.max_uses ? ` · ${t('security.uses', { n: grant.uses, max: grant.max_uses })}` : ''}
        </span>
        {grant.reason ? <span className="bidi">{grant.reason}</span> : null}
      </span>
      <button type="button" className="btn btn--sm btn--ghost" onClick={onRevoke} disabled={busy}>
        {t('security.revoke')}
      </button>
    </li>
  );
}

function AgentPermissionsCard({ id, perms, onChanged }: { id: AgentId; perms: AgentPermissions; onChanged: () => void }) {
  const { t, agentName } = useI18n();
  const api = useApi();
  const { run, isPending } = useRunner(t('common.requestFailed'));
  const name = agentName(id);
  return (
    <li className="perm-card" data-testid={`security-perms-${id}`}>
      <header className="perm-card__head">
        <AgentBadge agent={id} number={AGENT_NUMBERS[id]} />
        <span className="perm-card__name">{name}</span>
        <Chip tone={riskTone(perms.risk_ceiling)} title={t('security.riskCeiling')}>
          {t('security.riskCeiling')}: {t(riskKey(perms.risk_ceiling))}
        </Chip>
        <ConfirmButton className="btn btn--sm btn--ghost" confirmLabel={t('common.confirm')} title={t('security.resetConfirm', { name })} disabled={isPending('reset')} onConfirm={() => void run('reset', async () => {
          await api.resetAgentPermissions(id);
          onChanged();
        })}>
          <Icon name="restart" size={12} />
          {t('security.reset')}
        </ConfirmButton>
      </header>
      <ul className="chip-list">
        {perms.capabilities.map((cap) => (
          <li key={cap}>
            <Chip tone="steel" className="chip--mono">
              {cap}
            </Chip>
          </li>
        ))}
      </ul>
      {perms.restricted_to_task || perms.folder_scope?.length ? (
        <dl className="kv perm-card__limits">
          {perms.restricted_to_task ? (
            <>
              <dt>{t('security.restrictedTo')}</dt>
              <dd className="mono ltr">{perms.restricted_to_task}</dd>
            </>
          ) : null}
          {perms.folder_scope?.length ? (
            <>
              <dt>{t('security.folderScope')}</dt>
              <dd className="mono ltr">{perms.folder_scope.join(', ')}</dd>
            </>
          ) : null}
        </dl>
      ) : null}
      <p className="perm-card__grants-label">{t('security.grants')}</p>
      {perms.grants.length ? (
        <ul className="grants">
          {perms.grants.map((g) => (
            <GrantRow key={g.id} grant={g} busy={isPending(`revoke:${g.id}`)} onRevoke={() => void run(`revoke:${g.id}`, async () => {
              await api.revokeGrant(g.id);
              onChanged();
            })} />
          ))}
        </ul>
      ) : (
        <p className="hint">{t('security.noGrants')}</p>
      )}
    </li>
  );
}

function GrantForm({ capabilities, onGranted }: { capabilities: string[]; onGranted: () => void }) {
  const { t, agentName } = useI18n();
  const api = useApi();
  const toast = useToast();
  const [agent, setAgent] = useState<AgentId>('research');
  const [capability, setCapability] = useState(capabilities[0] ?? '');
  const [roots, setRoots] = useState('');
  const [taskId, setTaskId] = useState('');
  const [ttl, setTtl] = useState('60');
  const [maxUses, setMaxUses] = useState('');
  const [reason, setReason] = useState('');
  const { run, isPending } = useRunner(t('common.requestFailed'));
  useEffect(() => {
    if (!capability && capabilities.length) setCapability(capabilities[0]);
  }, [capabilities, capability]);
  const submit = () =>
    void run('grant', async () => {
      const minutes = Number(ttl);
      const uses = Number(maxUses);
      await api.grantPermission({
        agent_id: agent,
        capability,
        roots: roots.split('\n').map((r) => r.trim()).filter(Boolean),
        task_id: taskId.trim() || undefined,
        ttl_s: Number.isFinite(minutes) && minutes > 0 ? Math.round(minutes * 60) : undefined,
        max_uses: Number.isFinite(uses) && uses > 0 ? Math.round(uses) : undefined,
        reason: reason.trim() || undefined,
      });
      toast('success', t('security.granted'), `${agentName(agent)} · ${capability}`);
      setRoots('');
      setTaskId('');
      setReason('');
      onGranted();
    });
  return (
    <form
      className="grant-form"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
      data-testid="security-grant-form"
    >
      <label className="field">
        <span className="field__label">{t('security.agent')}</span>
        <select className="select" value={agent} onChange={(e) => setAgent(e.target.value as AgentId)}>
          {AGENT_IDS.map((id) => (
            <option key={id} value={id}>
              {agentName(id)}
            </option>
          ))}
        </select>
      </label>
      <label className="field">
        <span className="field__label">{t('security.capability')}</span>
        <select className="select ltr" value={capability} onChange={(e) => setCapability(e.target.value)}>
          {capabilities.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
      </label>
      <label className="field grant-form__wide">
        <span className="field__label">{t('security.roots')}</span>
        <textarea className="textarea ltr" rows={2} value={roots} onChange={(e) => setRoots(e.target.value)} placeholder="C:\Users\...\Documents" />
      </label>
      <label className="field">
        <span className="field__label">{t('security.ttl')}</span>
        <input className="input num" type="number" min={1} value={ttl} onChange={(e) => setTtl(e.target.value)} />
      </label>
      <label className="field">
        <span className="field__label">{t('security.maxUses')}</span>
        <input className="input num" type="number" min={1} value={maxUses} onChange={(e) => setMaxUses(e.target.value)} />
      </label>
      <label className="field">
        <span className="field__label">{t('security.taskId')}</span>
        <input className="input ltr" value={taskId} onChange={(e) => setTaskId(e.target.value)} />
      </label>
      <label className="field">
        <span className="field__label">{t('security.reason')}</span>
        <input className="input" value={reason} onChange={(e) => setReason(e.target.value)} />
      </label>
      <div className="grant-form__actions">
        <span className="hint">{t('security.grantHint')}</span>
        <button type="submit" className="btn btn--primary" disabled={!capability || isPending('grant')} data-testid="security-grant-submit">
          {isPending('grant') ? <span className="spinner spinner--xs" /> : <Icon name="key" size={14} />}
          {t('security.grant')}
        </button>
      </div>
    </form>
  );
}

function PermissionsPanel() {
  const { t } = useI18n();
  const perms = useResource((a) => a.getPermissions(), []);
  const [formOpen, setFormOpen] = useState(false);
  return (
    <Panel
      className="sec-perms"
      title={t('security.permissions')}
      icon="key"
      actions={
        <>
          <button type="button" className={`btn btn--sm ${formOpen ? 'btn--ghost' : ''}`} onClick={() => setFormOpen((v) => !v)} aria-expanded={formOpen} data-testid="security-grant-toggle">
            <Icon name={formOpen ? 'x' : 'plus'} size={13} />
            {t('security.grantForm')}
          </button>
          <button type="button" className="icon-btn icon-btn--sm" onClick={perms.reload} aria-label={t('common.refresh')} title={t('common.refresh')}>
            <Icon name="restart" size={13} />
          </button>
        </>
      }
      testId="security-permissions"
    >
      {formOpen && perms.data ? <GrantForm capabilities={perms.data.capabilities} onGranted={perms.reload} /> : null}
      {perms.error ? (
        <ErrorBox message={perms.error} onRetry={perms.reload} retryLabel={t('common.retry')} />
      ) : !perms.data ? (
        <Loading label={t('common.loading')} rows={5} />
      ) : (
        <ul className="perm-cards">
          {AGENT_IDS.map((id) => {
            const p = perms.data!.agents[id];
            return p ? <AgentPermissionsCard key={id} id={id} perms={p} onChanged={perms.reload} /> : null;
          })}
        </ul>
      )}
    </Panel>
  );
}

function AuditPanel() {
  const { t, agentName } = useI18n();
  const api = useApi();
  const dispatch = useDispatch();
  const audit = useAppState((s) => s.audit);
  const loaded = useAppState((s) => s.auditLoaded);
  const [decision, setDecision] = useState<string>('all');
  const [agent, setAgent] = useState<string>('all');
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (loaded) return;
    let alive = true;
    api
      .getAudit(300)
      .then((entries) => alive && dispatch({ type: 'audit.loaded', entries }))
      .catch((err) => alive && setError(err instanceof Error ? err.message : String(err)));
    return () => {
      alive = false;
    };
  }, [api, dispatch, loaded]);
  const rows = useMemo(
    () => audit.filter((e: AuditEntry) => (decision === 'all' || e.decision === decision) && (agent === 'all' || (agent === 'jarvis' ? !e.agent_id : e.agent_id === agent))),
    [audit, decision, agent],
  );
  return (
    <Panel
      className="sec-audit"
      title={t('security.audit')}
      icon="history"
      meta={<span className="num">{rows.length}</span>}
      actions={
        <>
          <label className="mini-select">
            <span className="sr-only">{t('security.col.agent')}</span>
            <Icon name="agents" size={13} />
            <select className="select select--sm" value={agent} onChange={(e) => setAgent(e.target.value)} data-testid="audit-agent">
              <option value="all">{t('activity.all')}</option>
              <option value="jarvis">JARVIS</option>
              {AGENT_IDS.map((id) => (
                <option key={id} value={id}>
                  {agentName(id)}
                </option>
              ))}
            </select>
          </label>
          <label className="mini-select">
            <span className="sr-only">{t('security.col.decision')}</span>
            <Icon name="filter" size={13} />
            <select className="select select--sm" value={decision} onChange={(e) => setDecision(e.target.value)} data-testid="audit-decision">
              <option value="all">{t('security.allDecisions')}</option>
              {DECISIONS.map((d) => (
                <option key={d} value={d}>
                  {t(`decision.${d}` as MessageKey)}
                </option>
              ))}
            </select>
          </label>
        </>
      }
      flush
      testId="security-audit"
    >
      {error && !loaded ? (
        <div className="panel-pad">
          <ErrorBox message={error} />
        </div>
      ) : !loaded && !audit.length ? (
        <div className="panel-pad">
          <Loading label={t('common.loading')} rows={6} />
        </div>
      ) : rows.length ? (
        <div className="table-wrap">
          <table className="table audit-table">
            <thead>
              <tr>
                <th>{t('security.col.time')}</th>
                <th>{t('security.col.agent')}</th>
                <th>{t('security.col.tool')}</th>
                <th>{t('security.col.action')}</th>
                <th>{t('security.col.risk')}</th>
                <th>{t('security.col.decision')}</th>
                <th>{t('security.col.outcome')}</th>
                <th>{t('security.col.duration')}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((e) => (
                <tr key={e.id} data-decision={e.decision} title={e.reason || e.error || undefined}>
                  <td className="num ltr">{formatTimeOf(e.ts)}</td>
                  <td className="truncate">{e.agent_id ? agentName(e.agent_id) : 'JARVIS'}</td>
                  <td>
                    <code className="ltr">{e.tool}</code>
                  </td>
                  <td className="audit-table__action bidi" dir={textDir(e.action)}>
                    <BidiText text={e.action} />
                    {e.error ? (
                      <span className="audit-table__error bidi">
                        {' · '}
                        <BidiText text={e.error} />
                      </span>
                    ) : null}
                  </td>
                  <td>
                    <Chip tone={riskTone(e.risk)}>{t(riskKey(e.risk))}</Chip>
                  </td>
                  <td>
                    <Chip tone={DECISION_TONE[e.decision] ?? 'steel'}>{t(`decision.${e.decision}` as MessageKey)}</Chip>
                  </td>
                  <td>
                    <Chip tone={OUTCOME_TONE[e.outcome] ?? 'steel'}>{t(`outcome.${e.outcome}` as MessageKey)}</Chip>
                  </td>
                  <td className="num ltr">{e.duration_ms !== null ? `${Math.round(e.duration_ms)} ms` : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <EmptyState icon="history" title={t('security.auditEmpty')} />
      )}
    </Panel>
  );
}

/** Approvals, kill switches, what each agent may do (and extra grants), and the audit trail. */
export function SecurityScreen({ onNavigate }: { onNavigate: Navigate }) {
  const { t } = useI18n();
  const mode = useAppState((s) => s.mode);
  return (
    <div className="screen screen--security" data-testid="security-screen">
      <ScreenHeader title={t('security.title')} subtitle={t('security.subtitle')} icon="security">
        <button type="button" className="chip chip--plain chip--lg chip--btn" onClick={() => onNavigate('settings')} title={t('security.policyHint')}>
          <Icon name="layers" size={12} />
          {t('topbar.mode', { mode: t(`mode.${mode}` as MessageKey) })}
        </button>
      </ScreenHeader>
      <div className="security-layout">
        <ApprovalsPanel />
        <KillSwitchesPanel />
        <PermissionsPanel />
        <AuditPanel />
      </div>
    </div>
  );
}
