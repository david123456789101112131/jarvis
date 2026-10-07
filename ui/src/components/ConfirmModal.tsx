import { useEffect, useRef, useState } from 'react';
import type { ConfirmationRequest } from '../api/types';
import { useI18n, type MessageKey } from '../i18n';
import { useApi, useDispatch, useNow, useToast, errorMessage } from '../state/context';
import { Icon } from './Icon';
import { riskTone } from './tones';
import { AgentBadge, Chip } from './ui';
import { BidiText } from './BidiText';

/** Detail keys the dialog names in words; any other key is shown as it is. */
export const DETAIL_KEYS = ['paths', 'path', 'items', 'permanent', 'command', 'shell', 'cwd', 'code', 'manager', 'packages', 'folders', 'examples', 'tool', 'params'] as const;

const MAX_DETAIL = 4000;

function clip(text: string): string {
  return text.length > MAX_DETAIL ? `${text.slice(0, MAX_DETAIL - 1)}…` : text;
}

/** One detail of an approval request, shown in full (the box scrolls): the user approves exactly this. */
export function DetailValue({ value }: { value: unknown }) {
  const { t } = useI18n();
  if (value === null || value === undefined || value === '') return <span>—</span>;
  if (typeof value === 'boolean') return <span>{t(value ? 'common.yes' : 'common.no')}</span>;
  if (typeof value === 'string' || typeof value === 'number') {
    return (
      <bdi dir="ltr" className="bidi-run mono confirm__value">
        {clip(String(value))}
      </bdi>
    );
  }
  if (Array.isArray(value) && value.every((v) => typeof v === 'string' || typeof v === 'number')) {
    return (
      <ul className="confirm__list">
        {value.map((v, i) => (
          <li key={i}>
            <bdi dir="ltr" className="bidi-run mono">
              {clip(String(v))}
            </bdi>
          </li>
        ))}
      </ul>
    );
  }
  let json: string;
  try {
    json = JSON.stringify(value, null, 2);
  } catch {
    json = String(value);
  }
  return (
    <pre className="confirm__json mono" dir="ltr">
      {clip(json)}
    </pre>
  );
}

export function secondsLeft(request: Pick<ConfirmationRequest, 'expires_at'>, now: number): number | null {
  if (!request.expires_at) return null;
  const end = Date.parse(request.expires_at);
  if (!Number.isFinite(end)) return null;
  return Math.max(0, Math.ceil((end - now) / 1000));
}

interface ConfirmModalProps {
  request: ConfirmationRequest;
  waiting: number;
  onLater: () => void;
}

/** Approval dialog for a `confirm.request`: risk-colored, with reasons, details and a countdown. */
export function ConfirmModal({ request, waiting, onLater }: ConfirmModalProps) {
  const { t, agentName } = useI18n();
  const api = useApi();
  const dispatch = useDispatch();
  const toast = useToast();
  const now = useNow(250);
  const [remember, setRemember] = useState(false);
  const [busy, setBusy] = useState<'approve' | 'deny' | null>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const denyRef = useRef<HTMLButtonElement>(null);
  const tone = riskTone(request.risk);
  const left = secondsLeft(request, now);
  const created = Date.parse(request.created_at);
  const total = request.expires_at && Number.isFinite(created) ? Math.max(1, (Date.parse(request.expires_at) - created) / 1000) : null;
  const fraction = left !== null && total ? Math.max(0, Math.min(1, left / total)) : 1;
  const canRemember = !!request.task_id && request.risk !== 'critical';
  const details = Object.entries(request.details ?? {}).filter(([key]) => key !== 'capability' || request.details.capability !== request.tool);

  useEffect(() => {
    setRemember(false);
    setBusy(null);
    denyRef.current?.focus();
  }, [request.id]);

  const answer = async (approved: boolean) => {
    setBusy(approved ? 'approve' : 'deny');
    try {
      await api.answerConfirmation(request.id, approved, approved && remember && canRemember);
      dispatch({ type: 'confirmation.resolved', id: request.id });
      toast(approved ? 'success' : 'info', approved ? t('confirm.approved') : t('confirm.denied'), request.summary);
    } catch (err) {
      toast('error', t('common.requestFailed'), errorMessage(err));
      setBusy(null);
    }
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      onLater();
      return;
    }
    if (e.key !== 'Tab' || !dialogRef.current) return;
    const focusable = dialogRef.current.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), [tabindex="0"]');
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  };

  return (
    <div className="modal-backdrop" onKeyDown={onKeyDown}>
      <div className="modal confirm" data-tone={tone} role="alertdialog" aria-modal="true" aria-labelledby="confirm-title" aria-describedby="confirm-summary" ref={dialogRef} data-testid="confirm-modal" data-risk={request.risk}>
        <div className="confirm__head">
          <span className="confirm__icon">
            <Icon name={request.risk === 'critical' || request.risk === 'high' ? 'alert' : 'shield'} size={24} />
          </span>
          <div className="confirm__titles">
            <h2 className="confirm__title" id="confirm-title">
              {t('confirm.title')}
            </h2>
            <p className="confirm__who">
              <AgentBadge agent={request.agent_id} number={agentNumber(request.agent_id)} size="sm" />
              <span>{t('confirm.agentWants', { agent: agentName(request.agent_id), tool: '' }).trim()}</span>
              <code className="confirm__tool ltr">{request.tool}</code>
            </p>
          </div>
          <Chip tone={tone} size="lg" testId="confirm-risk">
            {t('confirm.risk', { risk: t(`risk.${request.risk}` as MessageKey) })}
          </Chip>
        </div>

        <div className="confirm__body">
          <p className="confirm__summary bidi" id="confirm-summary">
            <BidiText text={request.summary} />
          </p>
          {request.reasons.length ? (
            <div className="confirm__section">
              <p className="section-label">{t('confirm.reasons')}</p>
              <ul className="confirm__reasons">
                {request.reasons.map((reason, i) => (
                  <li key={i} className="bidi">
                    <Icon name="alert" size={13} />
                    <span>
                      <BidiText text={reason} />
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
          {details.length ? (
            <div className="confirm__section">
              <p className="section-label">{t('confirm.details')}</p>
              <dl className="kv confirm__details">
                {details.map(([key, value]) => {
                  const named = (DETAIL_KEYS as readonly string[]).includes(key);
                  return (
                    <div key={key} className="kv__row" data-detail={key}>
                      <dt className={named ? undefined : 'ltr'}>{named ? t(`confirm.detail.${key}` as MessageKey) : key}</dt>
                      <dd>
                        <DetailValue value={value} />
                      </dd>
                    </div>
                  );
                })}
              </dl>
            </div>
          ) : null}
          {canRemember ? (
            <label className="check">
              <input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} data-testid="confirm-remember" />
              <span className="check__box" aria-hidden="true">
                <Icon name="check" size={12} strokeWidth={2.6} />
              </span>
              <span>{t('confirm.remember')}</span>
            </label>
          ) : null}
        </div>

        <div className="confirm__timer">
          <div className="confirm__timer-bar" style={{ transform: `scaleX(${fraction})` }} />
        </div>
        <div className="confirm__foot">
          <span className="confirm__countdown" data-testid="confirm-countdown">
            <Icon name="clock" size={13} />
            {left !== null ? t('confirm.expiresIn', { s: left }) : t('confirm.noExpiry')}
            {waiting > 0 ? <span className="confirm__more"> · {t('confirm.more', { n: waiting })}</span> : null}
          </span>
          <div className="confirm__actions">
            <button type="button" className="btn btn--ghost" onClick={onLater} data-testid="confirm-later">
              {t('confirm.later')}
            </button>
            <button type="button" ref={denyRef} className="btn btn--danger" disabled={busy !== null} onClick={() => void answer(false)} data-testid="confirm-deny">
              {busy === 'deny' ? <span className="spinner spinner--xs" /> : <Icon name="x" size={15} />}
              {t('confirm.deny')}
            </button>
            <button type="button" className="btn btn--ok" disabled={busy !== null} onClick={() => void answer(true)} data-testid="confirm-approve">
              {busy === 'approve' ? <span className="spinner spinner--xs" /> : <Icon name="check" size={15} />}
              {t('confirm.approve')}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

const NUMBERS: Record<string, number> = { research: 1, system: 2, development: 3, creative: 4, planner: 5 };

function agentNumber(id: string): number | string {
  return NUMBERS[id] ?? '•';
}
