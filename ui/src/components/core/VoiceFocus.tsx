import { useCallback, useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type TransitionEvent } from 'react';
import type { TaskStatus } from '../../api/types';
import { useI18n } from '../../i18n';
import { useApi, useAppState, useStore, useWakeAt } from '../../state/context';
import { selectCoreOverlay, selectFocus, selectOrbState, selectSubtasks, type FocusMode, type FocusView } from '../../state/selectors';
import type { AppState } from '../../state/types';
import { Icon } from '../Icon';
import { Orb } from '../orb/Orb';
import { ProgressBar } from '../ui';
import { setFocusHandOff } from './focusHandOff';
import { ActionCard, SpeechBubble, TranscriptBubble, WakeLabel } from './Overlays';

/** How long the view stays after the voice goes quiet (so a reply that follows does not bounce it away and back). */
export const FOCUS_LINGER_MS = 1400;
/** How long a change between talking and acting must hold before the orb moves (a 0.3 s action is not worth a trip). */
export const FOCUS_SETTLE_MS = 280;
/** The longest the orb's trip back to its place may take before the view is put away regardless. */
const LEAVE_FALLBACK_MS = 900;
const TOPBAR_H = 56;
const CAPTION_ROOM = 150;
const SMALL_ORB = 54;
const CARD_PAD = 14;
/** Room under the orb in the voice view's own window (the desktop shell's FOCUS_CAPTION). */
const WINDOW_CAPTION = 170;

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

/** Pure layout: `d` is the visible sphere when talking (its canvas is 1.5× larger for the rings and glow), centred
 *  with room for the caption below; acting, the same orb shrinks into the narration card at the top. */
export function focusGeometry(w: number, h: number, rtl: boolean) {
  const avail = Math.max(200, h - TOPBAR_H);
  const d = clamp(Math.min(w * 0.38, (avail - CAPTION_ROOM) / 1.08), 160, 560);
  const canvas = Math.round(d * 1.5);
  const composition = d * 1.04 + CAPTION_ROOM;
  const cy = TOPBAR_H + Math.max(16, (avail - composition) / 2) + d * 0.52;
  const cx = w / 2;
  const cardW = Math.min(540, w - 48);
  const cardLeft = (w - cardW) / 2;
  const cardTop = TOPBAR_H + 14;
  const sx = rtl ? cardLeft + cardW - CARD_PAD - SMALL_ORB / 2 : cardLeft + CARD_PAD + SMALL_ORB / 2;
  const sy = cardTop + CARD_PAD + SMALL_ORB / 2;
  return {
    d,
    canvas,
    cx,
    cy,
    captionTop: cy + d * 0.58,
    card: { left: cardLeft, top: cardTop, width: cardW },
    small: { dx: sx - cx, dy: sy - cy, scale: SMALL_ORB / d },
  };
}

/** The same in the voice view's own window, in the middle of the screen while JARVIS runs in the background:
 *  the orb fills the top square, the caption the strip below. */
export function windowGeometry(w: number, h: number): Geometry {
  const canvas = Math.max(120, Math.min(w, h - WINDOW_CAPTION));
  const d = canvas / 1.5;
  const cy = canvas / 2;
  return { d, canvas, cx: w / 2, cy, captionTop: cy + d * 0.6, card: { left: 0, top: 0, width: w }, small: { dx: 0, dy: 0, scale: 0.4 } };
}

type Geometry = ReturnType<typeof focusGeometry>;
type Stage = 'hidden' | 'entering' | 'shown' | 'leaving';
interface Home {
  transform: string;
  opacity: number;
  /** The orb comes out of (and goes back into) the command center's own orb, which hides meanwhile. */
  panel: boolean;
}

/** Where the orb comes from and goes back to: the command center's orb when it is on screen, else the middle. */
function homeOf(g: Geometry, own: boolean): Home {
  const el = !own && typeof document !== 'undefined' ? document.querySelector<HTMLElement>('.stage--panel [data-testid="orb"]') : null;
  const r = el?.getBoundingClientRect();
  if (r && r.width > 8 && r.bottom > 0 && r.right > 0 && r.top < window.innerHeight && r.left < window.innerWidth) {
    const x = r.left + r.width / 2 - g.cx;
    const y = r.top + r.height / 2 - g.cy;
    return { transform: `translate3d(${x}px, ${y}px, 0) scale(${r.width / g.canvas})`, opacity: 1, panel: true };
  }
  return { transform: 'translate3d(0, 28px, 0) scale(0.55)', opacity: 0, panel: false };
}

function placed(mode: FocusMode, g: Geometry): string {
  return mode === 'act' ? `translate3d(${g.small.dx}px, ${g.small.dy}px, 0) scale(${g.small.scale})` : 'translate3d(0, 0, 0) scale(1)';
}

/** The mode as shown: it comes at once, changes after FOCUS_SETTLE_MS and leaves after FOCUS_LINGER_MS. */
export function useSettledFocus(desired: FocusMode): FocusMode {
  const [mode, setMode] = useState<FocusMode>(desired);
  const comesNow = mode === 'off' && desired !== 'off';
  useEffect(() => {
    if (desired === mode) return;
    if (comesNow) {
      setMode(desired);
      return;
    }
    const id = setTimeout(() => setMode(desired), desired === 'off' ? FOCUS_LINGER_MS : FOCUS_SETTLE_MS);
    return () => clearTimeout(id);
  }, [desired, mode, comesNow]);
  return comesNow ? desired : mode;
}

function useWindowSize() {
  const read = () => ({ w: window.innerWidth, h: window.innerHeight });
  const [size, setSize] = useState(read);
  useEffect(() => {
    const onResize = () => setSize(read());
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);
  return size;
}

const SUBTASK_ICON: Partial<Record<TaskStatus, string>> = { completed: 'check', failed: 'x', skipped: 'minus', cancelled: 'x' };

function Narration({ focus, state }: { focus: FocusView; state: AppState }) {
  const { t } = useI18n();
  const { action, task } = focus;
  if (action && (action.status === 'running' || !task)) {
    return (
      <div className="focus-task" data-testid="focus-action">
        {action.status === 'running' ? <p className="focus-task__kicker">{t('focus.doing')}</p> : null}
        <ActionCard action={action} />
      </div>
    );
  }
  if (!task) return null;
  const subtasks = selectSubtasks(state, task.id).slice(0, 5);
  const current = subtasks.find((s) => s.status === 'running');
  const step = state.overlay.pipeline?.steps.find((s) => s.status === 'active')?.label ?? null;
  const waiting = task.status === 'waiting';
  return (
    <div className="focus-task" data-testid="focus-task" role="status" aria-live="polite">
      <p className="focus-task__kicker">{waiting ? t('focus.waiting') : t('focus.doing')}</p>
      <p className="focus-task__title bidi">{task.title}</p>
      {current || step ? <p className="focus-task__detail bidi">{current?.title ?? step}</p> : null}
      <ProgressBar value={task.progress} indeterminate={task.status === 'planning' || task.status === 'pending'} label={task.title} />
      {subtasks.length > 1 ? (
        <ol className="focus-task__steps" aria-label={t('focus.steps')}>
          {subtasks.map((s) => (
            <li key={s.id} className="focus-task__step" data-status={s.status}>
              <span className="focus-task__mark" aria-hidden="true">
                {s.status === 'running' ? <span className="spinner spinner--xs" /> : SUBTASK_ICON[s.status] ? <Icon name={SUBTASK_ICON[s.status]!} size={12} strokeWidth={2.4} /> : null}
              </span>
              <span className="bidi">{s.title}</span>
            </li>
          ))}
        </ol>
      ) : null}
    </div>
  );
}

/**
 * The voice view over the main window. While JARVIS listens, thinks or speaks, the orb comes out of the command
 * center, big in the middle with what is being said under it; while a spoken command is carried out it shrinks
 * into a card at the top that says what it is doing; then it goes back. Only transform and opacity move (the GPU
 * composites them), the orb is drawn at its big size and scaled down, and its own animation sleeps while hidden.
 * Esc or a click outside puts it away until JARVIS is called again.
 *
 * `standalone`: the same in its own transparent window in the middle of the screen (`?view=focus`), for when only
 * the mini orb is on screen. It asks the desktop shell to show the window before the orb comes and to hide it once
 * the orb is gone; while JARVIS acts it steps aside, and the mini orb in the corner says what is being done.
 */
export function VoiceFocus({ standalone = false }: { standalone?: boolean }) {
  const store = useStore();
  const api = useApi();
  const { t, dir } = useI18n();
  const voice = useAppState((s) => s.voice);
  useAppState((s) => s.overlay);
  useAppState((s) => s.tasks.active);
  useAppState((s) => s.liveSubtasks);
  useAppState((s) => s.taskDetails);
  useAppState((s) => s.killSwitch.global);
  const ui = useAppState((s) => s.ui);
  const assistantName = useAppState((s) => s.assistantName);
  const confirming = useAppState((s) => s.confirmations.some((c) => c.status === 'pending'));
  const size = useWindowSize();

  const now = Date.now();
  const full = store.getState();
  const focus = selectFocus(full, now);
  const core = selectCoreOverlay(full, now);
  const orbState = selectOrbState(full, now);
  const changes = [focus.nextChangeAt, core.nextChangeAt].filter((v): v is number => v !== null);
  useWakeAt(changes.length ? Math.min(...changes) : null);

  // In its own window acting means stepping aside (as if quiet: it stays through a quick action, and comes back at once).
  const settled = useSettledFocus(standalone && focus.mode === 'act' ? 'off' : focus.mode);
  const [dismissed, setDismissed] = useState(false);
  const lastVoice = useRef(voice.state);
  useEffect(() => {
    if (voice.state === 'wake' && lastVoice.current !== 'wake') setDismissed(false); // called again: it comes back
    lastVoice.current = voice.state;
  }, [voice.state]);
  useEffect(() => {
    if (settled === 'off') setDismissed(false);
  }, [settled]);
  const wanted: FocusMode = dismissed ? 'off' : settled;

  // Its own window must be on screen before the orb sets out (the shell shows it only while the mini orb is alone).
  const [windowUp, setWindowUp] = useState(!standalone);
  const asked = useRef(false);
  useEffect(() => {
    if (!standalone || wanted === 'off' || windowUp || asked.current) return;
    asked.current = true;
    api
      .windowAction('focus_show')
      .catch(() => undefined)
      .finally(() => {
        asked.current = false;
        setWindowUp(true);
      });
  }, [standalone, wanted, windowUp, api]);
  const mode: FocusMode = windowUp ? wanted : 'off';

  const g = standalone ? windowGeometry(size.w, size.h) : focusGeometry(size.w, size.h, dir === 'rtl');
  const orbRef = useRef<HTMLDivElement>(null);
  const [stage, setStage] = useState<Stage>('hidden');
  const [home, setHome] = useState<Home>({ transform: 'scale(0.55)', opacity: 0, panel: false });
  const shownMode = useRef<FocusMode>('talk'); // what the orb was doing before it left (it leaves from there)
  if (mode !== 'off') shownMode.current = mode;

  useLayoutEffect(() => {
    if (mode !== 'off') {
      if (stage === 'hidden') {
        setHome(homeOf(g, standalone));
        setStage('entering');
      } else if (stage === 'leaving') setStage('shown');
    } else if (stage === 'entering' || stage === 'shown') {
      setHome(homeOf(g, standalone));
      setStage('leaving');
    }
  }, [mode, stage]); // the layout is read only at the moment the orb sets out

  useLayoutEffect(() => {
    if (stage !== 'entering') return;
    orbRef.current?.getBoundingClientRect(); // lay the start in place, so the move is animated from it
    setStage('shown');
  }, [stage]);

  useEffect(() => {
    if (stage !== 'leaving') return;
    const id = setTimeout(() => setStage('hidden'), LEAVE_FALLBACK_MS);
    return () => clearTimeout(id);
  }, [stage]);

  const onTransitionEnd = useCallback(
    (e: TransitionEvent<HTMLDivElement>) => {
      if (e.target === orbRef.current && e.propertyName === 'transform' && stage === 'leaving') setStage('hidden');
    },
    [stage],
  );

  useEffect(() => {
    if (!standalone || stage !== 'hidden' || !windowUp || wanted !== 'off') return;
    setWindowUp(false); // the orb is gone: the window goes too, and the mini orb comes back
    api.windowAction('focus_hide').catch(() => undefined);
  }, [standalone, stage, windowUp, wanted, api]);

  const handOff = stage !== 'hidden' && home.panel;
  useEffect(() => {
    const root = document.documentElement;
    root.classList.toggle('focus-orb-on', handOff);
    setFocusHandOff(handOff);
    return () => {
      root.classList.remove('focus-orb-on');
      setFocusHandOff(false);
    };
  }, [handOff]);

  useEffect(() => {
    if (mode === 'off' || confirming) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !e.defaultPrevented) setDismissed(true);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [mode, confirming]);

  const getLevel = useCallback(() => store.getState().voiceLevel, [store]);
  const colorKey = ui.theme_color;
  const visualMode: FocusMode = stage === 'hidden' ? 'off' : mode === 'off' ? 'off' : mode;
  const orbStyle: CSSProperties = {
    width: g.canvas,
    height: g.canvas,
    left: g.cx - g.canvas / 2,
    top: g.cy - g.canvas / 2,
    transform: stage === 'entering' || stage === 'leaving' || stage === 'hidden' ? home.transform : placed(mode, g),
    opacity: stage === 'hidden' ? 0 : stage === 'shown' ? 1 : home.opacity,
    transition: stage === 'entering' ? 'none' : undefined,
  };

  const below = core.below;
  let caption = null;
  if (visualMode === 'talk' || (stage === 'leaving' && shownMode.current === 'talk')) {
    if (below?.kind === 'speech') caption = <SpeechBubble text={below.text} language={below.language} speaking={voice.state === 'speaking'} level={getLevel} colorKey={colorKey} />;
    else if (below?.kind === 'transcript') caption = <TranscriptBubble text={below.text} final={below.final} language={below.language} level={getLevel} colorKey={colorKey} />;
    else if (below?.kind === 'wake') caption = <WakeLabel name={assistantName || 'JARVIS'} level={getLevel} colorKey={colorKey} />;
    else if (voice.state === 'processing') caption = <p className="focus__thinking">{t('focus.thinking')}</p>;
  }
  const narrating = visualMode === 'act' || (stage === 'leaving' && shownMode.current === 'act');

  return (
    <div
      className={`focus ${standalone ? 'focus--window' : ''}`}
      data-testid="voice-focus"
      data-mode={visualMode}
      data-stage={stage}
      role="region"
      aria-label={t('focus.label')}
      aria-hidden={stage === 'hidden' ? true : undefined}
      onClick={standalone ? () => setDismissed(true) : undefined}
    >
      {standalone ? null : <div className="focus__backdrop" onClick={() => setDismissed(true)} aria-hidden="true" />}
      <div ref={orbRef} className="focus__orb" style={orbStyle} onTransitionEnd={onTransitionEnd} aria-hidden="true">
        <Orb state={orbState} theme={ui.theme_color} size={g.canvas} opacity={ui.orb_opacity} level={getLevel} paused={stage === 'hidden'} maxPixelRatio={1.25} testId="focus-orb" />
      </div>
      <div className="focus__caption" style={{ top: g.captionTop }} data-testid="focus-caption">
        {caption}
      </div>
      {standalone ? null : (
        <div className="focus__card" style={{ left: g.card.left, top: g.card.top, width: g.card.width, minHeight: SMALL_ORB + CARD_PAD * 2 }} data-testid="focus-narration">
          <div className="focus__card-body">{narrating ? <Narration focus={focus} state={full} /> : null}</div>
          <button type="button" className="icon-btn icon-btn--sm focus__close" onClick={() => setDismissed(true)} aria-label={t('focus.close')} title={t('focus.close')} data-testid="focus-close" tabIndex={narrating ? 0 : -1}>
            <Icon name="x" size={14} />
          </button>
        </div>
      )}
      {visualMode === 'talk' && !standalone ? (
        <button type="button" className="icon-btn focus__close focus__close--talk" onClick={() => setDismissed(true)} aria-label={t('focus.close')} title={t('focus.close')} data-testid="focus-close-talk">
          <Icon name="x" size={16} />
        </button>
      ) : null}
    </div>
  );
}
