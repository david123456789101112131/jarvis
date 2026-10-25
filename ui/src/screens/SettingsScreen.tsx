import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { AGENT_IDS, MODES, THEMES, type DeepPartialSettings, type SecretStatus, type SettingsObject, type Theme, type UISettings } from '../api/types';
import { useI18n, type MessageKey } from '../i18n';
import { useApi, useAppState, useDispatch, useResource, useRunner, useToast } from '../state/context';
import { Icon } from '../components/Icon';
import { Orb } from '../components/orb/Orb';
import { ScreenHeader } from '../components/shell/Shell';
import { Chip, ConfirmButton, EmptyState, ErrorBox, Loading, Panel, Segmented, Slider, Toggle } from '../components/ui';

/** Sections the core reads only at start-up (see RESTART_SECTIONS in jarvis/app.py). */
const RESTART_SECTIONS = new Set(['paths', 'server', 'memory']);

type Json = Record<string, unknown>;

export function getPath(obj: unknown, path: string): unknown {
  let cur: unknown = obj;
  for (const key of path.split('.')) {
    if (!cur || typeof cur !== 'object') return undefined;
    cur = (cur as Json)[key];
  }
  return cur;
}

/** {"a.b": 1, "a.c": 2} → {a: {b: 1, c: 2}} */
export function buildPatch(values: Record<string, unknown>): Json {
  const patch: Json = {};
  for (const [path, value] of Object.entries(values)) {
    const keys = path.split('.');
    let cur = patch;
    keys.slice(0, -1).forEach((k) => {
      cur[k] = (cur[k] as Json | undefined) ?? {};
      cur = cur[k] as Json;
    });
    cur[keys[keys.length - 1]] = value;
  }
  return patch;
}

type Option = { value: string; label: string };

type FieldDef =
  | { kind: 'text'; path: string; label: MessageKey; hint?: MessageKey; ltr?: boolean; placeholder?: string }
  | { kind: 'select'; path: string; label: MessageKey; hint?: MessageKey; options: Option[] }
  | { kind: 'toggle'; path: string; label: MessageKey; hint?: MessageKey; risky?: (value: boolean) => boolean }
  | { kind: 'number'; path: string; label: MessageKey; hint?: MessageKey; min: number; max: number; step?: number; integer?: boolean };

/** Draft values for a group of settings fields, saved together as one PATCH. */
function useSectionForm(settings: SettingsObject, fields: FieldDef[], onSaved: (next: SettingsObject) => void) {
  const { t } = useI18n();
  const api = useApi();
  const toast = useToast();
  const initial = useCallback(() => Object.fromEntries(fields.map((f) => [f.path, toDraft(f, getPath(settings, f.path))])), [fields, settings]);
  const [draft, setDraft] = useState<Record<string, unknown>>(initial);
  const [saving, setSaving] = useState(false);
  // When the saved settings change (saved here or elsewhere), drop the draft for unchanged fields.
  const base = useRef(settings);
  useEffect(() => {
    if (base.current === settings) return;
    base.current = settings;
    setDraft(initial());
  }, [settings, initial]);

  const changed = fields.filter((f) => draft[f.path] !== toDraft(f, getPath(settings, f.path)));
  const errors: Record<string, string> = {};
  for (const f of changed) {
    if (f.kind === 'number') {
      const n = Number(draft[f.path]);
      if (!Number.isFinite(n) || n < f.min || n > f.max || (f.integer && !Number.isInteger(n))) errors[f.path] = t('settings.range', { min: f.min, max: f.max });
    }
  }
  const risky = changed.some((f) => f.kind === 'toggle' && f.risky?.(draft[f.path] as boolean));

  const save = async () => {
    if (!changed.length || Object.keys(errors).length) return;
    setSaving(true);
    try {
      const values = Object.fromEntries(changed.map((f) => [f.path, fromDraft(f, draft[f.path])]));
      const next = await api.patchSettings(buildPatch(values) as DeepPartialSettings);
      onSaved(next);
      const restart = changed.some((f) => RESTART_SECTIONS.has(f.path.split('.')[0]));
      toast(restart ? 'warning' : 'success', t('settings.saved'), restart ? t('settings.restartNeeded') : '');
    } catch (err) {
      toast('error', t('common.requestFailed'), err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };
  return {
    draft,
    set: (path: string, value: unknown) => setDraft((d) => ({ ...d, [path]: value })),
    reset: () => setDraft(initial()),
    dirty: changed.length > 0,
    errors,
    risky,
    saving,
    save,
  };
}

function toDraft(field: FieldDef, value: unknown): unknown {
  if (field.kind === 'toggle') return Boolean(value);
  if (field.kind === 'number') return value === undefined || value === null ? '' : String(value);
  return value === undefined || value === null ? '' : String(value);
}

function fromDraft(field: FieldDef, value: unknown): unknown {
  if (field.kind === 'number') return Number(value);
  if (field.kind === 'toggle') return Boolean(value);
  return String(value ?? '').trim();
}

function FieldRow({ label, hint, children, error, wide }: { label: string; hint?: string; children: ReactNode; error?: string; wide?: boolean }) {
  return (
    <div className={`set-row ${wide ? 'set-row--wide' : ''}`}>
      <div className="set-row__text">
        <span className="set-row__label">{label}</span>
        {hint ? <span className="set-row__hint">{hint}</span> : null}
        {error ? <span className="set-row__error">{error}</span> : null}
      </div>
      <div className="set-row__control">{children}</div>
    </div>
  );
}

function FormFields({ fields, form }: { fields: FieldDef[]; form: ReturnType<typeof useSectionForm> }) {
  const { t } = useI18n();
  return (
    <>
      {fields.map((f) => {
        const value = form.draft[f.path];
        const hint = f.hint ? t(f.hint) : undefined;
        switch (f.kind) {
          case 'toggle':
            return (
              <FieldRow key={f.path} label={t(f.label)} hint={hint}>
                <Toggle checked={Boolean(value)} onChange={(v) => form.set(f.path, v)} label={t(f.label)} hideLabel testId={`set-${f.path}`} />
              </FieldRow>
            );
          case 'select':
            return (
              <FieldRow key={f.path} label={t(f.label)} hint={hint}>
                <select className="select" value={String(value)} onChange={(e) => form.set(f.path, e.target.value)} aria-label={t(f.label)} data-testid={`set-${f.path}`}>
                  {f.options.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>
              </FieldRow>
            );
          case 'number':
            return (
              <FieldRow key={f.path} label={t(f.label)} hint={hint} error={form.errors[f.path]}>
                <input className="input num set-row__number" type="number" min={f.min} max={f.max} step={f.step ?? 1} value={String(value)} onChange={(e) => form.set(f.path, e.target.value)} aria-label={t(f.label)} aria-invalid={form.errors[f.path] ? true : undefined} data-testid={`set-${f.path}`} />
              </FieldRow>
            );
          default:
            return (
              <FieldRow key={f.path} label={t(f.label)} hint={hint}>
                <input className={`input ${f.ltr ? 'ltr' : ''}`} value={String(value)} placeholder={f.placeholder} onChange={(e) => form.set(f.path, e.target.value)} aria-label={t(f.label)} spellCheck={false} data-testid={`set-${f.path}`} />
              </FieldRow>
            );
        }
      })}
    </>
  );
}

function SaveBar({ form }: { form: ReturnType<typeof useSectionForm> }) {
  const { t } = useI18n();
  if (!form.dirty) return null;
  const invalid = Object.keys(form.errors).length > 0;
  return (
    <div className="save-bar" data-testid="settings-save-bar">
      <span className="save-bar__msg">
        <Icon name="info" size={13} />
        {form.risky ? t('settings.riskyChange') : t('settings.unsaved')}
      </span>
      <button type="button" className="btn btn--sm btn--ghost" onClick={form.reset} disabled={form.saving}>
        {t('settings.discard')}
      </button>
      {form.risky ? (
        <ConfirmButton className="btn btn--sm btn--danger" confirmLabel={t('common.confirm')} onConfirm={() => void form.save()} disabled={form.saving || invalid} testId="settings-save">
          {t('settings.saveChanges')}
        </ConfirmButton>
      ) : (
        <button type="button" className="btn btn--sm btn--primary" onClick={() => void form.save()} disabled={form.saving || invalid} data-testid="settings-save">
          {form.saving ? <span className="spinner spinner--xs" /> : <Icon name="check" size={13} />}
          {t('settings.saveChanges')}
        </button>
      )}
    </div>
  );
}

function FormSection({ id, title, icon, fields, settings, onSaved, children, after }: { id: string; title: string; icon: string; fields: FieldDef[]; settings: SettingsObject; onSaved: (s: SettingsObject) => void; children?: ReactNode; after?: ReactNode }) {
  const form = useSectionForm(settings, fields, onSaved);
  return (
    <Panel as="section" className="set-section" title={title} icon={icon} testId={`settings-${id}`} footer={form.dirty ? <SaveBar form={form} /> : undefined}>
      <span id={`settings-${id}`} className="set-anchor" />
      {children}
      <div className="set-rows">
        <FormFields fields={fields} form={form} />
      </div>
      {after}
    </Panel>
  );
}

// ---------------------------------------------------------------- sections

function GeneralSection({ settings, onSaved }: { settings: SettingsObject; onSaved: (s: SettingsObject) => void }) {
  const { t, lang, setLang } = useI18n();
  const fields = useMemo<FieldDef[]>(
    () => [
      { kind: 'text', path: 'general.assistant_name', label: 'settings.assistantName' },
      { kind: 'text', path: 'general.user_name', label: 'settings.userName' },
      {
        kind: 'select',
        path: 'general.language',
        label: 'settings.responseLanguage',
        options: [
          { value: 'auto', label: t('settings.auto') },
          { value: 'he', label: t('lang.he') },
          { value: 'en', label: t('lang.en') },
        ],
      },
      { kind: 'select', path: 'general.default_mode', label: 'settings.defaultMode', options: MODES.map((m) => ({ value: m, label: t(`mode.${m}` as MessageKey) })) },
      { kind: 'toggle', path: 'general.resume_interrupted_on_start', label: 'settings.resumeOnStart', hint: 'settings.resumeOnStartHint' },
      { kind: 'toggle', path: 'general.start_with_windows', label: 'settings.startWithWindows', hint: 'settings.startWithWindowsHint' },
    ],
    [t],
  );
  return (
    <FormSection id="general" title={t('settings.general')} icon="user" fields={fields} settings={settings} onSaved={onSaved}>
      <div className="set-rows">
        <FieldRow label={t('settings.uiLanguage')}>
          <Segmented<'he' | 'en'>
            value={lang}
            onChange={setLang}
            label={t('settings.uiLanguage')}
            options={[
              { value: 'he', label: t('lang.he') },
              { value: 'en', label: t('lang.en') },
            ]}
            testId="set-ui-language"
          />
        </FieldRow>
      </div>
    </FormSection>
  );
}

function SecretRow({ name, status, onChanged }: { name: string; status: SecretStatus; onChanged: () => void }) {
  const { t } = useI18n();
  const api = useApi();
  const toast = useToast();
  const [value, setValue] = useState('');
  const { run, isPending } = useRunner(t('common.requestFailed'));
  const save = () =>
    void run('save', async () => {
      await api.putSecret(name, value.trim());
      setValue('');
      toast('success', t('settings.saved'), status.label);
      onChanged();
    });
  return (
    <li className="secret" data-testid={`secret-${name}`}>
      <div className="secret__head">
        <span className="secret__label">{status.label}</span>
        <code className="secret__name ltr">{name}</code>
        <Chip tone={status.configured ? 'ok' : 'muted'} dot>
          {status.configured ? t('settings.configured') : t('settings.notConfigured')}
        </Chip>
        {status.configured && status.source ? <span className="faint">{t('settings.source', { s: t(`settings.src.${status.source}` as MessageKey) })}</span> : null}
      </div>
      <form
        className="secret__form"
        onSubmit={(e) => {
          e.preventDefault();
          if (value.trim()) save();
        }}
      >
        <input className="input ltr" type="password" autoComplete="off" spellCheck={false} value={value} placeholder={t('settings.enterKey')} aria-label={`${status.label}: ${t('settings.enterKey')}`} onChange={(e) => setValue(e.target.value)} data-testid={`secret-input-${name}`} />
        <button type="submit" className="btn btn--sm btn--primary" disabled={!value.trim() || isPending('save')}>
          {t('settings.save')}
        </button>
        {status.configured && status.source !== 'env' ? (
          <ConfirmButton className="btn btn--sm btn--ghost" confirmLabel={t('common.confirm')} title={t('settings.removeConfirm', { name: status.label })} disabled={isPending('remove')} onConfirm={() => void run('remove', async () => {
            await api.deleteSecret(name);
            onChanged();
          })}>
            {t('settings.remove')}
          </ConfirmButton>
        ) : null}
      </form>
    </li>
  );
}

function SecretsBlock() {
  const { t } = useI18n();
  const secrets = useResource((a) => a.getSecrets(), []);
  return (
    <div className="secrets" data-testid="settings-secrets">
      <p className="section-label">{t('settings.apiKeys')}</p>
      <p className="hint">{t('settings.keyHint')}</p>
      {secrets.error ? (
        <ErrorBox message={secrets.error} onRetry={secrets.reload} retryLabel={t('common.retry')} />
      ) : !secrets.data ? (
        <Loading label={t('common.loading')} rows={3} />
      ) : (
        <ul className="secret-list">
          {Object.entries(secrets.data).map(([name, status]) => (
            <SecretRow key={name} name={name} status={status} onChanged={secrets.reload} />
          ))}
        </ul>
      )}
    </div>
  );
}

function ProvidersSection({ settings, onSaved }: { settings: SettingsObject; onSaved: (s: SettingsObject) => void }) {
  const { t } = useI18n();
  const providers = useAppState((s) => s.providers);
  const fields = useMemo<FieldDef[]>(
    () => [
      { kind: 'toggle', path: 'ai.allow_paid', label: 'settings.allowPaid', hint: 'settings.allowPaidHint', risky: (v) => v },
      { kind: 'toggle', path: 'ai.cloud_only_when_told', label: 'settings.cloudWhenTold', hint: 'settings.cloudWhenToldHint' },
      {
        kind: 'select',
        path: 'ai.provider',
        label: 'settings.provider',
        hint: 'settings.providerHint',
        options: [
          { value: 'auto', label: t('settings.auto') },
          { value: 'anthropic', label: 'Anthropic (Claude)' },
          { value: 'xai', label: 'xAI (Grok)' },
          { value: 'openai', label: 'OpenAI' },
          { value: 'local', label: t('settings.local') },
        ],
      },
      ...AGENT_IDS.map(
        (id, i): FieldDef => ({
          kind: 'select',
          path: `ai.agent_providers.${id}`,
          label: `settings.agentAI.${id}` as MessageKey,
          hint: i === 0 ? 'settings.agentAIHint' : undefined,
          options: [
            { value: 'xai', label: 'xAI (Grok)' },
            { value: 'anthropic', label: 'Anthropic (Claude)' },
            { value: 'openai', label: 'OpenAI' },
            { value: 'local', label: t('settings.local') },
            { value: 'auto', label: t('settings.agentAIAuto') },
          ],
        }),
      ),
      { kind: 'text', path: 'ai.xai.model', label: 'settings.xaiModel', ltr: true },
      { kind: 'text', path: 'ai.anthropic.model', label: 'settings.anthropicModel', ltr: true },
      { kind: 'toggle', path: 'ai.anthropic.fallbacks', label: 'settings.fallbacks', hint: 'settings.fallbacksHint' },
      { kind: 'toggle', path: 'ai.anthropic.use_server_web_search', label: 'settings.webSearch', hint: 'settings.webSearchHint' },
      { kind: 'text', path: 'ai.openai.model', label: 'settings.openaiModel', ltr: true },
      { kind: 'toggle', path: 'ai.local.enabled', label: 'settings.localEnabled', hint: 'settings.localHint' },
      { kind: 'text', path: 'ai.local.base_url', label: 'settings.baseUrl', ltr: true },
      { kind: 'text', path: 'ai.local.model', label: 'settings.localModel', ltr: true },
    ],
    [t],
  );
  const status: { key: string; label: string; on: boolean }[] = [
    { key: 'anthropic', label: 'Anthropic', on: providers.anthropic },
    { key: 'xai', label: 'xAI (Grok)', on: providers.xai },
    { key: 'openai', label: 'OpenAI', on: providers.openai },
    { key: 'local', label: t('settings.local'), on: providers.local },
  ];
  return (
    <FormSection id="providers" title={t('settings.providers')} icon="sparkles" fields={fields} settings={settings} onSaved={onSaved} after={<SecretsBlock />}>
      <div className="provider-status" data-testid="provider-status">
        <div className="provider-status__active">
          <span className="faint">{t('settings.activeProvider')}</span>
          <strong className="ltr">{providers.active ?? providers.when_told ?? t('settings.none')}</strong>
          {providers.active ? null : providers.when_told ? (
            <span className="hint" data-testid="provider-when-told">{t('settings.whenToldHint')}</span>
          ) : (
            <span className="hint">{t('settings.noProviderHint')}</span>
          )}
        </div>
        <ul className="provider-status__list">
          {status.map((p) => (
            <li key={p.key}>
              <Chip tone={p.on ? 'ok' : 'muted'} dot>
                {p.label} · {p.on ? t('settings.configured') : t('settings.notConfigured')}
              </Chip>
            </li>
          ))}
          <li>
            {/* Active = switched on AND a Claude key to use it with; on without a key says what is missing. */}
            <Chip tone={providers.fallbacks ? 'info' : 'muted'} testId="fallbacks-state">
              {t('settings.fallbacks')}:{' '}
              {providers.fallbacks ? t('settings.on') : getPath(settings, 'ai.anthropic.fallbacks') ? t('settings.fallbacksNeedKey') : t('settings.off')}
            </Chip>
          </li>
        </ul>
      </div>
    </FormSection>
  );
}

function VoiceSection({ settings, onSaved }: { settings: SettingsObject; onSaved: (s: SettingsObject) => void }) {
  const { t } = useI18n();
  const api = useApi();
  const voice = useAppState((s) => s.voice);
  const { run, isPending } = useRunner(t('common.requestFailed'));
  const fields = useMemo<FieldDef[]>(
    () => [
      { kind: 'toggle', path: 'voice.enabled', label: 'settings.voiceEnabled' },
      { kind: 'toggle', path: 'voice.always_listening', label: 'settings.alwaysListening', hint: 'settings.alwaysListeningHint' },
      { kind: 'toggle', path: 'voice.wake_by_name', label: 'settings.wakeByName', hint: 'settings.wakeByNameHint' },
      { kind: 'toggle', path: 'voice.ctrl_to_talk', label: 'settings.ctrlToTalk', hint: 'settings.ctrlToTalkHint' },
      { kind: 'toggle', path: 'voice.speak_responses', label: 'settings.speakResponses' },
      { kind: 'text', path: 'voice.wake_word_model', label: 'settings.wakeWord', hint: 'settings.wakeWordHint', ltr: true },
      {
        kind: 'select',
        path: 'voice.stt_language',
        label: 'settings.sttLanguage',
        options: [
          { value: 'auto', label: t('settings.auto') },
          { value: 'he', label: t('lang.he') },
          { value: 'en', label: t('lang.en') },
        ],
      },
      { kind: 'text', path: 'voice.tts_voice_he', label: 'settings.voiceHe', ltr: true },
      { kind: 'text', path: 'voice.tts_voice_en', label: 'settings.voiceEn', ltr: true },
      { kind: 'text', path: 'voice.input_device', label: 'settings.input', placeholder: t('settings.systemDefault') },
      { kind: 'text', path: 'voice.output_device', label: 'settings.output', placeholder: t('settings.systemDefault') },
    ],
    [t],
  );
  const stateKey = voice.enabled ? voice.state : 'disabled';
  return (
    <FormSection id="voice" title={t('settings.voice')} icon="mic" fields={fields} settings={settings} onSaved={onSaved}>
      <div className="voice-status" data-testid="voice-status">
        <dl className="kv">
          <dt>{t('settings.state')}</dt>
          <dd>{t(`voice.state.${stateKey}` as MessageKey)}</dd>
          <dt>{t('settings.engines')}</dt>
          <dd className="ltr mono">
            {t('settings.engineWake')}: {voice.engines.wake} · {t('settings.engineStt')}: {voice.engines.stt} · {t('settings.engineTts')}: {voice.engines.tts}
          </dd>
          <dt>{t('settings.input')}</dt>
          <dd className="bidi">{voice.devices.input ?? t('settings.systemDefault')}</dd>
          <dt>{t('settings.output')}</dt>
          <dd className="bidi">{voice.devices.output ?? t('settings.systemDefault')}</dd>
          {voice.last_transcript ? (
            <>
              <dt>{t('settings.lastHeard')}</dt>
              <dd className="bidi">{voice.last_transcript}</dd>
            </>
          ) : null}
        </dl>
        {voice.error ? <p className="notice notice--warn bidi">{voice.error}</p> : null}
        <button type="button" className="btn btn--sm" disabled={isPending('test')} onClick={() => void run('test', () => api.speak(t('settings.testPhrase')))} data-testid="voice-test">
          <Icon name="speaker" size={14} />
          {t('settings.testVoice')}
        </button>
      </div>
    </FormSection>
  );
}

const ORB_PATCH_DELAY = 350;

function OrbSection() {
  const { t } = useI18n();
  const api = useApi();
  const dispatch = useDispatch();
  const toast = useToast();
  const ui = useAppState((s) => s.ui);
  const pending = useRef<Partial<UISettings>>({});
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const flush = useCallback(() => {
    timer.current = null;
    const patch = pending.current;
    pending.current = {};
    if (!Object.keys(patch).length) return;
    api.patchSettings({ ui: patch }).catch((err) => toast('error', t('common.requestFailed'), err instanceof Error ? err.message : String(err)));
  }, [api, toast, t]);
  useEffect(
    () => () => {
      if (timer.current) {
        clearTimeout(timer.current);
        flush();
      }
    },
    [flush],
  );
  const change = (patch: Partial<UISettings>) => {
    dispatch({ type: 'ui.local', patch }); // instant preview (the orb window follows the "settings" event)
    pending.current = { ...pending.current, ...patch };
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(flush, ORB_PATCH_DELAY);
  };
  return (
    <Panel as="section" className="set-section set-section--orb" title={t('settings.orb')} icon="core" testId="settings-orb">
      <span id="settings-orb" className="set-anchor" />
      <div className="orb-custom">
        <div className="orb-custom__preview" aria-label={t('settings.preview')}>
          <Orb state="idle" theme={ui.theme_color} size={Math.round(150 * ui.orb_size)} opacity={ui.orb_opacity} quality="low" testId="settings-orb-preview" />
          <span className="orb-custom__caption">{t('settings.preview')}</span>
        </div>
        <div className="set-rows">
          <FieldRow label={t('settings.orbSize')}>
            <div className="slider-row">
              <Slider value={ui.orb_size} min={0.5} max={1.6} step={0.05} onChange={(v) => change({ orb_size: v })} label={t('settings.orbSize')} format={(v) => `${Math.round(v * 100)}%`} testId="set-orb-size" />
              <span className="num">{Math.round(ui.orb_size * 100)}%</span>
            </div>
          </FieldRow>
          <FieldRow label={t('settings.orbOpacity')}>
            <div className="slider-row">
              <Slider value={ui.orb_opacity} min={0.3} max={1} step={0.02} onChange={(v) => change({ orb_opacity: v })} label={t('settings.orbOpacity')} format={(v) => `${Math.round(v * 100)}%`} testId="set-orb-opacity" />
              <span className="num">{Math.round(ui.orb_opacity * 100)}%</span>
            </div>
          </FieldRow>
          <FieldRow label={t('settings.mainColor')} wide>
            <div className="swatches" role="radiogroup" aria-label={t('settings.mainColor')}>
              {THEMES.map((theme: Theme) => (
                <button key={theme} type="button" role="radio" aria-checked={ui.theme_color === theme} className="swatch" data-theme-color={theme} onClick={() => change({ theme_color: theme })} title={`${t(`theme.${theme}` as MessageKey)} — ${t(`theme.desc.${theme}` as MessageKey)}`} data-testid={`swatch-${theme}`}>
                  <span className="swatch__dot" aria-hidden="true" />
                  <span className="swatch__label">{t(`theme.${theme}` as MessageKey)}</span>
                  <span className="swatch__desc">{t(`theme.desc.${theme}` as MessageKey)}</span>
                </button>
              ))}
            </div>
          </FieldRow>
          <FieldRow label={t('settings.followMouse')}>
            <Toggle checked={ui.follow_mouse} onChange={(v) => change({ follow_mouse: v })} label={t('settings.followMouse')} hideLabel testId="set-follow-mouse" />
          </FieldRow>
          <FieldRow label={t('settings.alwaysActive')} hint={t('settings.alwaysActiveHint')}>
            <Toggle checked={ui.always_active} onChange={(v) => change({ always_active: v })} label={t('settings.alwaysActive')} hideLabel testId="set-always-active" />
          </FieldRow>
          <FieldRow label={t('settings.miniOrb')} hint={t('settings.miniOrbHint')}>
            <Toggle checked={ui.mini_orb} onChange={(v) => change({ mini_orb: v })} label={t('settings.miniOrb')} hideLabel testId="set-mini-orb" />
          </FieldRow>
          <FieldRow label={t('settings.focusOrb')} hint={t('settings.focusOrbHint')}>
            <Toggle checked={ui.focus_orb} onChange={(v) => change({ focus_orb: v })} label={t('settings.focusOrb')} hideLabel testId="set-focus-orb" />
          </FieldRow>
        </div>
      </div>
    </Panel>
  );
}

function SafetySection({ settings, onSaved }: { settings: SettingsObject; onSaved: (s: SettingsObject) => void }) {
  const { t } = useI18n();
  const fields = useMemo<FieldDef[]>(
    () => [
      { kind: 'number', path: 'security.confirm_timeout_s', label: 'settings.confirmTimeout', hint: 'settings.confirmTimeoutHint', min: 15, max: 3600, integer: true },
      { kind: 'toggle', path: 'security.use_recycle_bin', label: 'settings.recycleBin', hint: 'settings.recycleBinHint', risky: (v) => !v },
      { kind: 'number', path: 'security.broad_delete_threshold', label: 'settings.broadDelete', hint: 'settings.broadDeleteHint', min: 1, max: 10000, integer: true },
      { kind: 'toggle', path: 'security.network_enabled', label: 'settings.network', hint: 'settings.networkHint' },
      { kind: 'toggle', path: 'security.shell_enabled', label: 'settings.shell', hint: 'settings.shellHint' },
      { kind: 'toggle', path: 'security.autonomous_preapprove_high', label: 'settings.preapproveHigh', hint: 'settings.preapproveHint', risky: (v) => v },
    ],
    [],
  );
  return (
    <FormSection id="safety" title={t('settings.safety')} icon="shield" fields={fields} settings={settings} onSaved={onSaved}>
      <p className="notice notice--info">
        <Icon name="shield" size={14} />
        {t('settings.safetyNote')}
      </p>
    </FormSection>
  );
}

function ExecutionSection({ settings, onSaved }: { settings: SettingsObject; onSaved: (s: SettingsObject) => void }) {
  const { t } = useI18n();
  const fields = useMemo<FieldDef[]>(
    () => [
      { kind: 'number', path: 'orchestrator.max_parallel_subtasks', label: 'settings.maxParallel', min: 1, max: 16, integer: true },
      { kind: 'number', path: 'orchestrator.max_retries', label: 'settings.maxRetries', hint: 'settings.maxRetriesHint', min: 0, max: 5, integer: true },
      { kind: 'toggle', path: 'orchestrator.verify_results', label: 'settings.verify', hint: 'settings.verifyHint' },
      { kind: 'toggle', path: 'agents.autostart', label: 'settings.autostart' },
    ],
    [],
  );
  return <FormSection id="execution" title={t('settings.execution')} icon="tasks" fields={fields} settings={settings} onSaved={onSaved} />;
}

function FoldersSection({ settings, onSaved }: { settings: SettingsObject; onSaved: (s: SettingsObject) => void }) {
  const { t } = useI18n();
  const files = useAppState((s) => s.files);
  const fields = useMemo<FieldDef[]>(
    () => [
      { kind: 'text', path: 'paths.workspace_dir', label: 'settings.workspace', hint: 'settings.folderHint', ltr: true, placeholder: files.workspace },
      { kind: 'text', path: 'paths.projects_dir', label: 'settings.projectsDir', hint: 'settings.folderHint', ltr: true, placeholder: files.projects },
      { kind: 'number', path: 'server.port', label: 'settings.port', hint: 'settings.portHint', min: 1024, max: 65535, integer: true },
    ],
    [files.workspace, files.projects],
  );
  return (
    <FormSection id="folders" title={t('settings.folders')} icon="folder" fields={fields} settings={settings} onSaved={onSaved}>
      <p className="notice notice--warn">
        <Icon name="restart" size={14} />
        {t('settings.restartNeeded')}
      </p>
    </FormSection>
  );
}

function AboutSection() {
  const { t } = useI18n();
  const api = useApi();
  const version = useAppState((s) => s.version || s.serverVersion || '—');
  const { run, isPending } = useRunner(t('common.requestFailed'));
  return (
    <Panel as="section" className="set-section" title={t('settings.about')} icon="info" testId="settings-about">
      <span id="settings-about" className="set-anchor" />
      <dl className="kv">
        <dt>{t('settings.version')}</dt>
        <dd className="num ltr">JARVIS {version}</dd>
      </dl>
      <div className="about-actions">
        <ConfirmButton className="btn btn--sm btn--danger" confirmLabel={t('settings.quitConfirm')} disabled={isPending('quit')} onConfirm={() => void run('quit', () => api.windowAction('quit'))} testId="settings-quit">
          <Icon name="power" size={13} />
          {t('settings.quit')}
        </ConfirmButton>
      </div>
    </Panel>
  );
}

const NAV: { id: string; label: MessageKey; icon: string }[] = [
  { id: 'general', label: 'settings.general', icon: 'user' },
  { id: 'providers', label: 'settings.providers', icon: 'sparkles' },
  { id: 'voice', label: 'settings.voice', icon: 'mic' },
  { id: 'orb', label: 'settings.orb', icon: 'core' },
  { id: 'safety', label: 'settings.safety', icon: 'shield' },
  { id: 'execution', label: 'settings.execution', icon: 'tasks' },
  { id: 'folders', label: 'settings.folders', icon: 'folder' },
  { id: 'about', label: 'settings.about', icon: 'info' },
];

/** Everything configurable from the UI. Each section saves its own changes; keys are write-only. */
export function SettingsScreen() {
  const { t } = useI18n();
  const settings = useResource((a) => a.getSettings(), []);
  const onSaved = useCallback((next: SettingsObject) => settings.setData(() => next), [settings]);
  const jump = (id: string) => document.getElementById(`settings-${id}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  return (
    <div className="screen screen--settings" data-testid="settings-screen">
      <ScreenHeader title={t('settings.title')} subtitle={t('settings.subtitle')} icon="settings" />
      <div className="settings-layout">
        <nav className="settings-nav" aria-label={t('settings.title')}>
          <ul>
            {NAV.map((n) => (
              <li key={n.id}>
                <button type="button" className="settings-nav__item" onClick={() => jump(n.id)}>
                  <Icon name={n.icon} size={15} />
                  {t(n.label)}
                </button>
              </li>
            ))}
          </ul>
        </nav>
        <div className="settings-sections">
          {settings.error ? (
            <Panel title={t('settings.loadFailed')} icon="alert">
              <ErrorBox message={settings.error} onRetry={settings.reload} retryLabel={t('common.retry')} />
            </Panel>
          ) : !settings.data ? (
            <Panel title={t('settings.title')} icon="settings">
              <Loading label={t('common.loading')} rows={8} />
            </Panel>
          ) : (
            <>
              <GeneralSection settings={settings.data} onSaved={onSaved} />
              <ProvidersSection settings={settings.data} onSaved={onSaved} />
              <VoiceSection settings={settings.data} onSaved={onSaved} />
              <OrbSection />
              <SafetySection settings={settings.data} onSaved={onSaved} />
              <ExecutionSection settings={settings.data} onSaved={onSaved} />
              <FoldersSection settings={settings.data} onSaved={onSaved} />
              <AboutSection />
            </>
          )}
          {settings.data && !Object.keys(settings.data).length ? <EmptyState icon="settings" title={t('settings.loadFailed')} /> : null}
        </div>
      </div>
    </div>
  );
}
