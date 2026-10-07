import { useState } from 'react';
import { useI18n, type MessageKey } from '../../i18n';
import { useApi, useAppState, useRunner } from '../../state/context';
import { CoreStage } from '../../components/core/CoreStage';
import { MicButton } from '../../components/core/MicButton';
import { Icon } from '../../components/Icon';
import { VOICE_TONE } from '../../components/tones';
import { Panel, StatusChip } from '../../components/ui';

const DOCK_HEIGHT = 84;

/** JARVIS core: the orb with live voice overlays and the push-to-talk dock. */
export function CorePanel({ className }: { className?: string }) {
  const { t } = useI18n();
  const api = useApi();
  const [toolsOpen, setToolsOpen] = useState(false);
  const voice = useAppState((s) => s.voice);
  const activeTasks = useAppState((s) => s.tasks.active.length);
  const { run, isPending } = useRunner(t('common.requestFailed'));
  const voiceKey = voice.enabled ? voice.state : 'disabled';
  const cancellable = voice.state === 'wake' || voice.state === 'listening' || voice.state === 'processing' || voice.state === 'speaking';

  return (
    <Panel
      className={`core-panel ${className ?? ''}`}
      title={t('core.title')}
      icon="core"
      testId="panel-core"
      flush
      meta={<StatusChip status={voiceKey} tone={VOICE_TONE[voiceKey]} label={t(`voice.state.${voiceKey}` as MessageKey)} size="lg" testId="core-voice-state" />}
      actions={
        activeTasks > 0 ? (
          <span className="core-panel__tasks">
            <span className="spinner spinner--xs" aria-hidden="true" />
            {t('core.activeTasks', { n: activeTasks })}
          </span>
        ) : null
      }
    >
      <div className="core-panel__body">
        <div className="core-panel__hud core-panel__hud--start ltr" aria-hidden="true">
          <span>WAKE · {voice.engines.wake}</span>
          <span>STT · {voice.engines.stt}</span>
          <span>TTS · {voice.engines.tts}</span>
        </div>
        <div className="core-panel__hud core-panel__hud--end ltr" aria-hidden="true">
          <span>{(voice.wake_word || 'jarvis').toUpperCase()}</span>
          <span>{voice.muted ? 'MIC · MUTED' : voice.enabled ? 'MIC · ARMED' : 'MIC · OFF'}</span>
        </div>
        <CoreStage variant="panel" toolsOpen={toolsOpen} onToggleTools={() => setToolsOpen((open) => !open)} onCloseTools={() => setToolsOpen(false)} reserveBottom={DOCK_HEIGHT} />
        <div className="core-dock" role="toolbar" aria-label={t('core.title')}>
          <button
            type="button"
            className={`dock-btn ${voice.enabled ? '' : 'dock-btn--off'}`}
            onClick={() => void run('power', () => api.voice(voice.enabled ? 'stop' : 'start'))}
            disabled={isPending('power')}
            aria-label={voice.enabled ? t('core.voiceOff') : t('core.voiceOn')}
            title={voice.enabled ? t('core.voiceOff') : t('core.voiceOn')}
            data-testid="voice-power"
          >
            <Icon name="power" size={17} />
          </button>
          <button
            type="button"
            className={`dock-btn ${voice.muted ? 'dock-btn--danger' : ''}`}
            onClick={() => void run('mute', () => api.voice(voice.muted ? 'unmute' : 'mute'))}
            disabled={!voice.enabled || isPending('mute')}
            aria-pressed={voice.muted}
            aria-label={voice.muted ? t('core.unmute') : t('core.mute')}
            title={voice.muted ? t('core.unmute') : t('core.mute')}
            data-testid="mute-button"
          >
            <Icon name={voice.muted ? 'mic-off' : 'volume'} size={17} />
          </button>
          <MicButton size="lg" />
          <button
            type="button"
            className={`dock-btn ${toolsOpen ? 'dock-btn--active' : ''}`}
            onClick={() => setToolsOpen((open) => !open)}
            aria-expanded={toolsOpen}
            aria-label={toolsOpen ? t('core.closeTools') : t('core.quickTools')}
            title={toolsOpen ? t('core.closeTools') : t('core.quickTools')}
            data-testid="tools-button"
          >
            <Icon name={toolsOpen ? 'x' : 'grid'} size={17} />
          </button>
          <button
            type="button"
            className="dock-btn"
            onClick={() => void run('cancel', () => api.voice('cancel'))}
            disabled={!cancellable || isPending('cancel')}
            aria-label={t('core.cancel')}
            title={t('core.cancel')}
            data-testid="voice-cancel"
          >
            <Icon name="stop" size={15} />
          </button>
        </div>
      </div>
    </Panel>
  );
}
