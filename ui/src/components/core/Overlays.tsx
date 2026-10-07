import type { ActionEvent } from '../../api/types';
import { useI18n, type MessageKey } from '../../i18n';
import { textDir } from '../../lib/format';
import type { PipelineStepState } from '../../state/types';
import { Icon, ICON_NAMES } from '../Icon';
import { ProgressBar } from '../ui';
import { Waveform } from '../Waveform';

export function TranscriptBubble({ text, final, language, level, colorKey, compact }: { text: string; final: boolean; language: string; level: () => number; colorKey: string; compact?: boolean }) {
  const { t } = useI18n();
  const empty = !text;
  return (
    <div className={`bubble bubble--transcript ${compact ? 'bubble--compact' : ''}`} data-testid="transcript-bubble" role="status" aria-live="polite">
      <p className={`bubble__text ${empty ? 'bubble__text--placeholder' : ''}`} dir={empty ? undefined : textDir(text, language)}>
        {empty ? t('core.listening') : text}
        {!final && !empty ? <span className="caret" aria-hidden="true" /> : null}
      </p>
      <Waveform level={level} active={!final} variant="bars" width={compact ? 180 : 230} height={22} colorKey={colorKey} className="bubble__wave" />
    </div>
  );
}

const STEP_ICONS: Record<string, string> = {
  transcribing: 'wave',
  understanding: 'sparkles',
  routing: 'network',
  selecting_tool: 'target',
  preparing_action: 'zap',
  executing: 'play',
};

export function PipelineSteps({ steps }: { steps: PipelineStepState[] }) {
  const { t } = useI18n();
  return (
    <ol className="steps" data-testid="pipeline-steps" aria-label={t('voice.state.processing')}>
      {steps.map((s) => (
        <li key={s.step} className="steps__item" data-status={s.status}>
          <span className="steps__icon">
            <Icon name={STEP_ICONS[s.step] ?? 'dots'} size={13} />
          </span>
          <span className="steps__label">{t(`step.${s.step}` as MessageKey)}</span>
          <span className="steps__state" aria-hidden="true">
            {s.status === 'done' ? <Icon name="check" size={14} strokeWidth={2.4} /> : s.status === 'active' ? <span className="spinner spinner--xs" /> : s.status === 'error' ? <Icon name="x" size={13} strokeWidth={2.4} /> : null}
          </span>
        </li>
      ))}
    </ol>
  );
}

// Core action icons whose glyph has another name here; any other name is used as is when it exists.
const ACTION_ICONS: Record<string, string> = { web: 'globe', close: 'x', 'file-search': 'search' };

export function ActionCard({ action, compact }: { action: ActionEvent; compact?: boolean }) {
  const icon = ACTION_ICONS[action.icon ?? ''] ?? (action.icon && ICON_NAMES.includes(action.icon) ? action.icon : 'app');
  const error = action.status === 'error';
  const done = action.status === 'done';
  const info = action.status === 'info';
  return (
    <div className={`action-card ${error ? 'action-card--error' : ''} ${done ? 'action-card--done' : ''} ${info ? 'action-card--info' : ''} ${compact ? 'action-card--compact' : ''}`} data-testid="action-card" data-status={action.status} role="status" aria-live="polite">
      <span className="action-card__icon" data-icon={action.icon}>
        <Icon name={icon} size={compact ? 20 : 24} />
      </span>
      <div className="action-card__body">
        <p className="action-card__title bidi">{action.title}</p>
        {action.detail ? <p className="action-card__detail bidi">{action.detail}</p> : null}
        {info ? null : <ProgressBar value={done ? 1 : action.progress ?? 0} tone={error ? 'danger' : done ? 'ok' : undefined} indeterminate={!error && !done && action.progress === undefined} label={action.title} />}
      </div>
      {done || info ? (
        <span className="action-card__state">
          <Icon name={done ? 'check-circle' : 'info'} size={18} />
        </span>
      ) : null}
    </div>
  );
}

export function SpeechBubble({ text, language, speaking, level, colorKey, compact }: { text: string; language: string; speaking: boolean; level: () => number; colorKey: string; compact?: boolean }) {
  const { t } = useI18n();
  return (
    <div className={`bubble bubble--speech ${compact ? 'bubble--compact' : ''}`} data-testid="speech-bubble" role="status" aria-live="polite" aria-label={t('core.speaking')}>
      <span className={`bubble__speaker ${speaking ? 'is-speaking' : ''}`}>
        <Icon name="speaker" size={20} />
      </span>
      <p className="bubble__text" dir={textDir(text, language)}>
        {text}
      </p>
      {speaking && !compact ? <Waveform level={level} active variant="bars" width={64} height={22} colorKey={colorKey} className="bubble__wave bubble__wave--mini" /> : null}
    </div>
  );
}

export function ErrorCard({ message, detail, onDismiss, compact }: { message: string | null; detail: string | null; onDismiss?: () => void; compact?: boolean }) {
  const { t } = useI18n();
  const title = message || t('core.errorGeneric');
  return (
    <div className={`error-card ${compact ? 'error-card--compact' : ''}`} data-testid="error-card" role="alert">
      <span className="error-card__icon">
        <Icon name="alert" size={compact ? 22 : 28} strokeWidth={1.8} />
      </span>
      <div className="error-card__body">
        <p className="error-card__title bidi">{title}</p>
        <p className="error-card__detail bidi">{detail || t('core.errorHint')}</p>
      </div>
      {onDismiss ? (
        <button type="button" className="icon-btn icon-btn--sm" onClick={onDismiss} aria-label={t('core.dismiss')} title={t('core.dismiss')}>
          <Icon name="x" size={14} />
        </button>
      ) : null}
    </div>
  );
}

export function WakeLabel({ name, level, colorKey }: { name: string; level: () => number; colorKey: string }) {
  return (
    <div className="wake-label" data-testid="wake-label">
      <span className="wake-label__name">{name}</span>
      <Waveform level={level} active variant="bars" width={150} height={26} colorKey={colorKey} />
    </div>
  );
}

export function IdleWave({ level, colorKey, label }: { level: () => number; colorKey: string; label: string }) {
  return (
    <div className="idle-wave" data-testid="idle-wave" title={label}>
      <Waveform level={level} active variant="line" width={190} height={18} colorKey={colorKey} />
      <span className="idle-wave__label">{label}</span>
    </div>
  );
}
