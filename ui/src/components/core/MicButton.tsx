import { useRef, useState } from 'react';
import { useApi, useAppState, useToast, errorMessage } from '../../state/context';
import { useI18n } from '../../i18n';
import { Icon } from '../Icon';

const HOLD_MS = 350;

/**
 * Push-to-talk: press and hold to talk (release stops), or click once to start and again to stop.
 * Keyboard: Enter/Space toggles.
 */
export function MicButton({ size = 'lg' }: { size?: 'lg' | 'md' }) {
  const api = useApi();
  const toast = useToast();
  const { t } = useI18n();
  const voice = useAppState((s) => s.voice);
  const level = useAppState((s) => (s.voice.state === 'listening' ? s.voiceLevel : 0));
  const [latched, setLatched] = useState(false);
  const pressedAt = useRef<number | null>(null);
  const listening = voice.state === 'listening' || voice.state === 'wake';
  const enabled = voice.enabled && voice.state !== 'disabled';

  const call = (action: 'ptt_start' | 'ptt_stop' | 'start') =>
    api.voice(action).catch((err) => toast('error', t('common.requestFailed'), errorMessage(err)));

  const onPointerDown = (e: React.PointerEvent) => {
    if (e.button !== 0 || !enabled) return;
    if (latched || listening) {
      pressedAt.current = null;
      setLatched(false);
      void call('ptt_stop');
      return;
    }
    pressedAt.current = performance.now();
    (e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId);
    void call('ptt_start');
  };

  const onPointerUp = () => {
    if (pressedAt.current === null) return;
    const held = performance.now() - pressedAt.current;
    pressedAt.current = null;
    if (held >= HOLD_MS) void call('ptt_stop');
    else setLatched(true);
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    e.preventDefault();
    if (!enabled) {
      void call('start');
      return;
    }
    if (listening || latched) {
      setLatched(false);
      void call('ptt_stop');
    } else {
      setLatched(true);
      void call('ptt_start');
    }
  };

  if (!enabled) {
    return (
      <button type="button" className={`mic-btn mic-btn--${size} mic-btn--off`} onClick={() => void call('start')} aria-label={t('core.voiceOn')} title={t('core.voiceOn')} data-testid="mic-button">
        <Icon name="mic-off" size={size === 'lg' ? 24 : 20} />
      </button>
    );
  }

  return (
    <button
      type="button"
      className={`mic-btn mic-btn--${size} ${listening ? 'is-live' : ''} ${voice.muted ? 'is-muted' : ''}`}
      style={{ '--lvl': level.toFixed(2) } as React.CSSProperties}
      onPointerDown={onPointerDown}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
      onKeyDown={onKeyDown}
      aria-pressed={listening}
      aria-label={listening ? t('core.micStop') : t('core.micStart')}
      title={t('core.micHint')}
      data-testid="mic-button"
    >
      <span className="mic-btn__ring" aria-hidden="true" />
      <Icon name={voice.muted ? 'mic-off' : 'mic'} size={size === 'lg' ? 24 : 20} />
    </button>
  );
}
