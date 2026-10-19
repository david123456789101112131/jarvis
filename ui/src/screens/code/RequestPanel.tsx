import { useState } from 'react';
import { estimateUsd, money, type DevEngine, type EngineMode, type RankedFile } from '../../api/dev';
import { BidiText } from '../../components/BidiText';
import { Icon } from '../../components/Icon';
import { Panel, Segmented } from '../../components/ui';
import { Unavailable } from '../../components/work/JobCard';
import { useI18n, type MessageKey } from '../../i18n';

export interface PickedFile extends RankedFile {
  picked: boolean;
}

/** The request, the files that go with it, and the AI that plans it — with what will be sent and its price. */
export function RequestPanel({
  enabled,
  request,
  onRequest,
  errorText,
  onErrorText,
  files,
  onToggleFile,
  onAddFile,
  listing,
  finding,
  onFind,
  engines,
  engine,
  onEngine,
  mode,
  onMode,
  pricesChecked,
  needs,
  planning,
  onPlan,
}: {
  enabled: boolean;
  request: string;
  onRequest: (text: string) => void;
  errorText: string;
  onErrorText: (text: string) => void;
  files: PickedFile[];
  onToggleFile: (path: string, picked: boolean) => void;
  onAddFile: (path: string) => boolean;
  listing: string[];
  finding: boolean;
  onFind: () => void;
  engines: DevEngine[];
  engine: DevEngine | undefined;
  onEngine: (provider: string) => void;
  mode: EngineMode;
  onMode: (mode: EngineMode) => void;
  pricesChecked: string;
  needs: string[];
  planning: boolean;
  onPlan: () => void;
}) {
  const { t } = useI18n();
  const [showError, setShowError] = useState(Boolean(errorText));
  const [adding, setAdding] = useState('');
  const [addError, setAddError] = useState('');
  const picked = files.filter((f) => f.picked);
  const estimate = estimateUsd(engine, picked.map((f) => f.size), request.length + errorText.length, mode);
  const name = engine?.name ?? 'AI';
  const canPlan = enabled && Boolean(engine) && request.trim().length >= 2 && !planning;

  return (
    <Panel title={t('code.request')} icon="code" testId="code-request">
      <div className="area__stack">
        <textarea
          className="textarea"
          value={request}
          onChange={(e) => onRequest(e.target.value)}
          placeholder={t('code.requestPlaceholder')}
          aria-label={t('code.request')}
          disabled={!enabled}
          rows={4}
          maxLength={4000}
          data-testid="code-request-text"
        />
        {showError ? (
          <label className="field">
            <span className="field__label">{t('code.errorLabel')}</span>
            <textarea
              className="textarea mono"
              dir="ltr"
              value={errorText}
              onChange={(e) => onErrorText(e.target.value)}
              placeholder={t('code.errorPlaceholder')}
              rows={5}
              maxLength={20000}
              disabled={!enabled}
              data-testid="code-error-text"
            />
          </label>
        ) : (
          <button type="button" className="btn btn--sm btn--ghost code-inline-btn" disabled={!enabled} onClick={() => setShowError(true)} data-testid="code-error-toggle">
            <Icon name="plus" size={13} />
            {t('code.errorToggle')}
          </button>
        )}

        <div className="actions-row">
          <button type="button" className="btn btn--sm" disabled={!enabled || finding || !(request.trim() || errorText.trim())} onClick={onFind} data-testid="code-find">
            <Icon name="search" size={13} />
            {finding ? t('code.finding') : t('code.findFiles')}
          </button>
        </div>

        <div className="code-files" data-testid="code-files">
          <span className="field__label">{t('code.filesTitle')}</span>
          {files.length ? (
            <ul>
              {files.map((f) => (
                <li key={f.path} className="code-files__item" data-picked={f.picked || undefined}>
                  <label>
                    <input type="checkbox" checked={f.picked} onChange={(e) => onToggleFile(f.path, e.target.checked)} data-testid={`code-file-${f.path}`} />
                    <span className="mono" dir="ltr">{f.path}</span>
                  </label>
                  <span className="code-files__why">
                    {f.reasons.slice(0, 2).map((r, i) => (
                      <BidiText key={i} text={r} />
                    ))}
                    {f.lines.length ? <span className="num">{t('code.lines', { lines: f.lines.join(', ') })}</span> : null}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="field__hint">{t('code.filesNone')}</p>
          )}
          <p className="field__hint">{t('code.filesHint')}</p>
          <form
            className="code-files__add"
            onSubmit={(e) => {
              e.preventDefault();
              const path = adding.trim().replace(/\\/g, '/');
              if (!path) return;
              if (onAddFile(path)) {
                setAdding('');
                setAddError('');
              } else setAddError(t('code.notInProject'));
            }}
          >
            <input
              className="input ltr"
              dir="ltr"
              list="code-listing"
              value={adding}
              onChange={(e) => setAdding(e.target.value)}
              placeholder={t('code.addFilePlaceholder')}
              aria-label={t('code.addFile')}
              disabled={!enabled}
              data-testid="code-add-file"
            />
            <datalist id="code-listing">
              {listing.slice(0, 2000).map((p) => (
                <option key={p} value={p} />
              ))}
            </datalist>
            <button type="submit" className="btn btn--sm" disabled={!enabled || !adding.trim()} data-testid="code-add-file-go">
              {t('code.add')}
            </button>
            {addError ? <span className="code-error-inline" role="alert">{addError}</span> : null}
          </form>
        </div>

        {engines.length ? (
          <>
            <div className="field-row">
              {engines.length > 1 ? (
                <label className="field">
                  <span className="field__label">{t('code.engine')}</span>
                  <select className="select" value={engine?.provider} onChange={(e) => onEngine(e.target.value)} data-testid="code-engine">
                    {engines.map((e) => (
                      <option key={e.provider} value={e.provider}>
                        {e.name} {e.model ? `(${e.model})` : ''}
                      </option>
                    ))}
                  </select>
                </label>
              ) : null}
              <div className="field">
                <span className="field__label">{t('code.mode')}</span>
                <Segmented
                  value={mode}
                  onChange={onMode}
                  label={t('code.mode')}
                  testId="code-mode"
                  options={(['auto', 'fast', 'strong'] as const).map((m) => ({ value: m, label: t(`code.mode.${m}` as MessageKey), title: t(`code.mode.${m}Hint` as MessageKey) }))}
                />
              </div>
            </div>
            <p className="field__hint">{t(`code.mode.${mode}Hint` as MessageKey)}</p>

            <div className="code-send" data-testid="code-will-send">
              <span className="field__label">
                <Icon name={engine?.local ? 'lock' : 'send'} size={13} /> {t('code.willSend')}
              </span>
              <ul>
                <li>{t('code.willSend.request')}</li>
                {errorText.trim() ? <li>{t('code.willSend.error')}</li> : null}
                <li>{t('code.willSend.facts')}</li>
                <li>{picked.length ? t('code.willSend.files', { n: picked.length }) : t('code.willSend.auto')}</li>
              </ul>
              <p>{engine?.local ? t('code.willSend.local') : t('code.willSend.where', { name, model: engine?.model ?? '' })}</p>
              <p className="field__hint">{t('code.willSend.secrets')}</p>
              {estimate ? (
                <p data-testid="code-price">
                  <strong>{t('code.price')}: </strong>
                  <span className="num">
                    ≈ {money(estimate[0])}–{money(estimate[1])}
                  </span>
                  {engine?.price ? <span className="field__hint"> · {t('code.priceList', { in: `$${engine.price[0]}`, out: `$${engine.price[1]}`, date: pricesChecked })}</span> : null}
                </p>
              ) : null}
            </div>

            <button type="button" className="btn btn--primary" disabled={!canPlan} onClick={onPlan} title={request.trim() ? undefined : t('code.needsRequest')} data-testid="code-plan">
              <Icon name="sparkles" size={14} />
              {planning ? t('code.planning') : t('code.plan', { name })}
            </button>
          </>
        ) : (
          <Unavailable title={t('code.noEngine')} reason={t('code.noEngineHint')} needs={needs} testId="code-no-engine" />
        )}
      </div>
    </Panel>
  );
}
