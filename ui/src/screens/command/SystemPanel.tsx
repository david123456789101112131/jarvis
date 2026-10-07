import { useMemo } from 'react';
import { useI18n } from '../../i18n';
import { formatDuration, formatGb, formatRate } from '../../lib/format';
import { useAppState } from '../../state/context';
import { RingGauge, Sparkline } from '../../components/charts';
import { Icon } from '../../components/Icon';
import type { Tone } from '../../components/tones';
import { Loading, Panel } from '../../components/ui';

export function loadTone(percent: number): Tone {
  if (percent >= 90) return 'danger';
  if (percent >= 75) return 'warn';
  return 'accent';
}

/** CPU / RAM / disk / network with client-side rolling history (from `system.stats`). */
export function SystemPanel({ className }: { className?: string }) {
  const { t, lang } = useI18n();
  const system = useAppState((s) => s.system);
  const history = useAppState((s) => s.systemHistory);
  const series = useMemo(() => {
    const tail = history.slice(-40);
    return {
      cpu: tail.map((h) => h.cpu),
      mem: tail.map((h) => h.mem),
      disk: tail.map((h) => h.disk),
      up: tail.map((h) => h.up),
      down: tail.map((h) => h.down),
    };
  }, [history]);

  const battery = system?.battery ?? null;
  const meta = system ? (
    <span className="sys-meta">
      {battery ? (
        <span className="sys-meta__item" title={battery.plugged ? t('system.charging') : t('system.battery')} data-testid="battery">
          <Icon name={battery.plugged ? 'bolt' : 'battery'} size={13} />
          <span className="num">{Math.round(battery.percent)}%</span>
        </span>
      ) : null}
      <span className="sys-meta__item" title={t('system.uptime')}>
        <Icon name="clock" size={12} />
        <span className="num">{formatDuration(system.uptime_s, lang)}</span>
      </span>
    </span>
  ) : null;

  const disk = system?.disks[0];
  return (
    <Panel className={className} title={t('system.title')} icon="cpu" meta={meta} testId="panel-system" bodyClassName="sys-panel__body">
      {!system ? (
        <Loading label={t('system.waiting')} rows={3} />
      ) : (
        <div className="sys-grid">
          <div className="sys-tile" data-testid="sys-cpu">
            <RingGauge value={system.cpu.percent} tone={loadTone(system.cpu.percent)} label={t('system.cpu')}>
              <span className="num">{Math.round(system.cpu.percent)}</span>
            </RingGauge>
            <div className="sys-tile__info">
              <span className="sys-tile__label">{t('system.cpu')}</span>
              <span className="sys-tile__value num">{Math.round(system.cpu.percent)}%</span>
              <span className="sys-tile__sub num ltr">{system.cpu.cores}C{system.cpu.freq_mhz ? ` · ${(system.cpu.freq_mhz / 1000).toFixed(1)}GHz` : ''}</span>
            </div>
            <Sparkline values={series.cpu} max={100} tone={loadTone(system.cpu.percent)} className="sys-tile__spark" height={26} />
          </div>
          <div className="sys-tile" data-testid="sys-ram">
            <RingGauge value={system.memory.percent} tone={loadTone(system.memory.percent)} label={t('system.ram')}>
              <span className="num">{Math.round(system.memory.percent)}</span>
            </RingGauge>
            <div className="sys-tile__info">
              <span className="sys-tile__label">{t('system.ram')}</span>
              <span className="sys-tile__value num ltr">
                {system.memory.used_gb.toFixed(1)}
                <small>/{Math.round(system.memory.total_gb)}GB</small>
              </span>
            </div>
            <Sparkline values={series.mem} max={100} tone={loadTone(system.memory.percent)} className="sys-tile__spark" height={26} />
          </div>
          <div className="sys-tile" data-testid="sys-disk">
            <RingGauge value={disk?.percent ?? 0} tone={loadTone(disk?.percent ?? 0)} label={t('system.disk')}>
              <span className="num">{Math.round(disk?.percent ?? 0)}</span>
            </RingGauge>
            <div className="sys-tile__info">
              <span className="sys-tile__label">
                {t('system.disk')} <span className="sys-tile__mount ltr">{disk?.mount ?? ''}</span>
              </span>
              <span className="sys-tile__value num ltr">
                {disk ? formatGb(disk.used_gb).replace(' GB', '') : '—'}
                <small>/{disk ? formatGb(disk.total_gb).replace(' ', '') : ''}</small>
              </span>
            </div>
            <Sparkline values={series.disk} max={100} tone="verify" className="sys-tile__spark" height={26} />
          </div>
          <div className="sys-tile sys-tile--net" data-testid="sys-net">
            <div className="sys-tile__info">
              <span className="sys-tile__label">{t('system.network')}</span>
              <span className="sys-net">
                <span className="sys-net__row" title={t('system.down')}>
                  <Icon name="arrow-down" size={12} />
                  <span className="num ltr">{formatRate(system.network.down_kbps)}</span>
                </span>
                <span className="sys-net__row sys-net__row--up" title={t('system.up')}>
                  <Icon name="arrow-up" size={12} />
                  <span className="num ltr">{formatRate(system.network.up_kbps)}</span>
                </span>
              </span>
            </div>
            <Sparkline values={series.down} secondary={series.up} floor={64} tone="work" className="sys-tile__spark" height={26} />
          </div>
        </div>
      )}
    </Panel>
  );
}
