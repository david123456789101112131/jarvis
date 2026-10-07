import { useCallback, useMemo } from 'react';
import { useI18n } from '../../i18n';
import { useElementSize } from '../../lib/hooks';
import { useAppState, useDispatch, useStore, useWakeAt } from '../../state/context';
import { HOLD, selectCoreOverlay, selectOrbState } from '../../state/selectors';
import { Orb } from '../orb/Orb';
import { Waveform } from '../Waveform';
import { ActionCard, ErrorCard, IdleWave, PipelineSteps, SpeechBubble, TranscriptBubble, WakeLabel } from './Overlays';
import { QuickToolsRing } from './QuickTools';

interface CoreStageProps {
  variant: 'panel' | 'mini';
  toolsOpen: boolean;
  onToggleTools: () => void;
  onCloseTools: () => void;
  /** Pixels kept free at the bottom of the stage (e.g. for the control dock). */
  reserveBottom?: number;
}

const STEPS_WIDTH = 196;

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

/**
 * Pure layout of the stage. `d` is the visible sphere diameter; the canvas is 1.5× larger so
 * the rings and glow have room. The bubble slot overlaps the lower part of the sphere.
 */
export function stageGeometry(w: number, h: number, opts: { mini: boolean; orbSize: number; reserveBottom: number; steps: boolean }) {
  const ha = Math.max(80, h - opts.reserveBottom);
  const belowRoom = opts.mini ? 64 : 92;
  const fit = Math.min(w * (opts.mini ? 0.66 : 0.56), (ha - belowRoom - (opts.mini ? 6 : 16)) / 1.02);
  const d = clamp(fit * opts.orbSize, opts.mini ? 56 : 90, Math.max(opts.mini ? 56 : 90, Math.min(w * 0.82, ha * 0.92)));
  const orbPx = d * 1.5;
  const composition = 1.02 * d + belowRoom;
  const top = Math.max(opts.mini ? 2 : 8, (ha - composition) / 2);
  const cy = top + 0.62 * d;
  const cx = w / 2;
  const shift = opts.steps && !opts.mini ? clamp(STEPS_WIDTH + 30 - (w - d) / 2, 0, w * 0.16) : 0;
  const toolsRadius = Math.max(d * 0.42, Math.min(d / 2 + (opts.mini ? 30 : 46), cy - (opts.mini ? 26 : 34), ha - cy - (opts.mini ? 34 : 46), w / 2 - (opts.mini ? 26 : 40)));
  return { d, orbPx, cx, cy, belowTop: cy + d * 0.4, shift, toolsRadius };
}

/** Orb + voice overlays (transcript, steps, action, speech, error) + quick-tools ring. */
export function CoreStage({ variant, toolsOpen, onToggleTools, onCloseTools, reserveBottom = 0 }: CoreStageProps) {
  const store = useStore();
  const dispatch = useDispatch();
  const { t, dir } = useI18n();
  const [ref, box] = useElementSize<HTMLDivElement>();

  // Subscribe to exactly the slices the overlay depends on (not the 15 Hz level).
  const overlay = useAppState((s) => s.overlay);
  const voice = useAppState((s) => s.voice);
  const ui = useAppState((s) => s.ui);
  useAppState((s) => s.killSwitch.global);
  useAppState((s) => s.tasks.active);
  const assistantName = useAppState((s) => s.assistantName);

  const now = Date.now();
  const full = store.getState();
  const view = selectCoreOverlay(full, now);
  const orbState = selectOrbState(full, now);
  const orbErrorEnd = overlay.error ? overlay.error.at + HOLD.orbError : null;
  const wakeCandidates = [view.nextChangeAt, orbErrorEnd !== null && orbErrorEnd > now ? orbErrorEnd : null].filter((v): v is number => v !== null);
  useWakeAt(wakeCandidates.length ? Math.min(...wakeCandidates) : null);

  const getLevel = useCallback(() => store.getState().voiceLevel, [store]);
  const mini = variant === 'mini';
  const w = box.width;
  const h = box.height;
  const ready = w > 0 && h > 0;
  const colorKey = ui.theme_color;
  const below = view.below;
  const showSteps = !mini && !!view.steps;
  const g = stageGeometry(w, h, { mini, orbSize: ui.orb_size, reserveBottom, steps: showSteps });
  const shiftX = (dir === 'rtl' ? -1 : 1) * g.shift;

  const belowNode = useMemo(() => {
    if (!below) return null;
    switch (below.kind) {
      case 'transcript':
        return <TranscriptBubble text={below.text} final={below.final} language={below.language} level={getLevel} colorKey={colorKey} compact={mini} />;
      case 'action':
        return <ActionCard action={below.action} compact={mini} />;
      case 'speech':
        return <SpeechBubble text={below.text} language={below.language} speaking={voice.state === 'speaking'} level={getLevel} colorKey={colorKey} compact={mini} />;
      case 'error':
        return <ErrorCard message={below.message} detail={below.detail} compact={mini} onDismiss={mini ? undefined : () => dispatch({ type: 'overlay.dismiss', at: Date.now() })} />;
      case 'wake':
        return <WakeLabel name={assistantName || 'JARVIS'} level={getLevel} colorKey={colorKey} />;
      case 'idle-wave':
        return mini ? null : <IdleWave level={getLevel} colorKey={colorKey} label={t('core.alwaysActive', { word: voice.wake_word || 'Jarvis' })} />;
      default:
        return null;
    }
  }, [below, getLevel, colorKey, mini, voice.state, voice.wake_word, assistantName, dispatch, t]);

  const bandW = Math.round(g.d * 1.32);
  const bandH = Math.round(Math.max(28, g.d * 0.22));

  return (
    <div
      ref={ref}
      className={`stage stage--${variant} ${toolsOpen ? 'stage--tools' : ''}`}
      data-testid="core-stage"
      data-voice-state={voice.state}
      data-orb-state={orbState}
      data-overlay={below?.kind ?? 'none'}
    >
      {ready ? (
        <>
          <div className="stage__focus" style={{ transform: `translateX(${shiftX}px)` }}>
            {!mini ? <div className="stage__reticle" style={{ left: g.cx, top: g.cy, width: g.d * 1.16, height: g.d * 1.16 }} aria-hidden="true" /> : null}
            <button
              type="button"
              className="stage__orb"
              style={{ width: g.orbPx, height: g.orbPx, left: g.cx - g.orbPx / 2, top: g.cy - g.orbPx / 2 }}
              onClick={onToggleTools}
              aria-label={toolsOpen ? t('core.closeTools') : t('core.quickTools')}
              aria-expanded={toolsOpen}
              data-testid="orb-button"
            >
              <Orb state={orbState} theme={ui.theme_color} size={g.orbPx} opacity={ui.orb_opacity} followMouse={ui.follow_mouse} level={getLevel} testId="orb" className="stage__orb-canvas" quality={mini ? 'low' : 'high'} />
            </button>
            {view.band && !toolsOpen ? (
              <div className="stage__band" style={{ left: g.cx, top: g.cy, width: bandW, height: bandH }}>
                <Waveform level={getLevel} active variant="band" width={bandW} height={bandH} colorKey={colorKey} testId="wave-band" />
              </div>
            ) : null}
            {belowNode && !toolsOpen ? (
              <div className={`stage__below stage__below--${below?.kind}`} style={{ top: g.belowTop }} key={below?.kind}>
                {belowNode}
              </div>
            ) : null}
            <div className="stage__tools" style={{ top: g.cy, left: g.cx }}>
              <QuickToolsRing open={toolsOpen} radius={g.toolsRadius} onClose={onCloseTools} />
            </div>
          </div>
          {showSteps ? (
            <div className="stage__steps" style={{ top: g.cy }}>
              <PipelineSteps steps={view.steps!} />
            </div>
          ) : null}
        </>
      ) : null}
    </div>
  );
}
