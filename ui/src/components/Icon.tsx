import type { CSSProperties, ReactElement } from 'react';

// Minimal stroke icon set (24×24 grid, 1.7px strokes) drawn for the HUD.
const ICONS: Record<string, ReactElement> = {
  core: (
    <>
      <circle cx="12" cy="12" r="4.2" />
      <ellipse cx="12" cy="12" rx="10" ry="4" transform="rotate(-25 12 12)" />
      <circle cx="12" cy="12" r="8.6" opacity=".35" />
    </>
  ),
  agents: (
    <>
      <circle cx="9" cy="8" r="3.2" />
      <path d="M3.5 19.5c.6-3.3 2.8-5 5.5-5s4.9 1.7 5.5 5" />
      <circle cx="16.8" cy="9.2" r="2.5" />
      <path d="M16 14.2c2.3 0 4 1.4 4.5 4.3" />
    </>
  ),
  tasks: (
    <>
      <path d="M9 6h11M9 12h11M9 18h11" />
      <path d="M3.5 6l1.4 1.4L7.3 5M3.5 12l1.4 1.4 2.4-2.4" />
      <circle cx="5" cy="18" r="1.3" />
    </>
  ),
  memory: (
    <>
      <ellipse cx="12" cy="5.5" rx="7.5" ry="2.8" />
      <path d="M4.5 5.5v6.2c0 1.6 3.4 2.8 7.5 2.8s7.5-1.2 7.5-2.8V5.5" />
      <path d="M4.5 11.7v6.2c0 1.6 3.4 2.8 7.5 2.8s7.5-1.2 7.5-2.8v-6.2" />
    </>
  ),
  security: (
    <>
      <path d="M12 3l7.5 3v5.6c0 4.6-3.2 8.2-7.5 9.4-4.3-1.2-7.5-4.8-7.5-9.4V6L12 3z" />
      <path d="M9 12l2.2 2.2L15.5 10" />
    </>
  ),
  settings: (
    <>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" />
    </>
  ),
  mic: (
    <>
      <rect x="9" y="3" width="6" height="11" rx="3" />
      <path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21M8.5 21h7" />
    </>
  ),
  'mic-off': (
    <>
      <path d="M15 9.3V6a3 3 0 0 0-5.7-1.3M9 9v2a3 3 0 0 0 4.6 2.5" />
      <path d="M18.5 11a6.4 6.4 0 0 1-1 3.4M5.5 11a6.5 6.5 0 0 0 10.2 5.3M12 17.5V21M8.5 21h7M3 3l18 18" />
    </>
  ),
  volume: (
    <>
      <path d="M4 9.5h3.5L12 5.5v13l-4.5-4H4z" />
      <path d="M15.5 9a4.2 4.2 0 0 1 0 6M18 6.5a7.8 7.8 0 0 1 0 11" />
    </>
  ),
  'volume-off': (
    <>
      <path d="M4 9.5h3.5L12 5.5v13l-4.5-4H4z" />
      <path d="M16 9.5l5 5M21 9.5l-5 5" />
    </>
  ),
  x: <path d="M6 6l12 12M18 6L6 18" />,
  check: <path d="M5 12.5l4.2 4.2L19 7" />,
  'check-circle': (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M8 12.3l2.7 2.7L16.2 9.5" />
    </>
  ),
  'x-circle': (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M9 9l6 6M15 9l-6 6" />
    </>
  ),
  play: <path d="M7 4.8v14.4L19 12z" />,
  pause: <path d="M8 5v14M16 5v14" />,
  stop: <rect x="6" y="6" width="12" height="12" rx="2" />,
  power: <path d="M12 3v8M6.6 6.6a7.5 7.5 0 1 0 10.8 0" />,
  restart: (
    <>
      <path d="M20 11a8 8 0 1 0-2.3 5.7" />
      <path d="M20 4v7h-7" />
    </>
  ),
  replay: (
    <>
      <path d="M4 12a8 8 0 1 0 2.3-5.7" />
      <path d="M4 4v5h5" />
      <path d="M10.5 9.5v5l4-2.5z" />
    </>
  ),
  octagon: (
    <>
      <path d="M8.2 3h7.6L21 8.2v7.6L15.8 21H8.2L3 15.8V8.2z" />
      <path d="M12 7.5v5.5M12 16.2v.3" />
    </>
  ),
  hand: (
    <>
      <path d="M8 13V6.5a1.5 1.5 0 0 1 3 0V12M11 11.5V5a1.5 1.5 0 0 1 3 0v6.5M14 12V6.5a1.5 1.5 0 0 1 3 0V14" />
      <path d="M17 10.5a1.5 1.5 0 0 1 3 0V15a6 6 0 0 1-6 6h-1.5a6 6 0 0 1-4.6-2.2L5 15.5a1.6 1.6 0 0 1 2.4-2.1L8 14" />
    </>
  ),
  plus: <path d="M12 5v14M5 12h14" />,
  minus: <path d="M5 12h14" />,
  trash: (
    <>
      <path d="M4 7h16M9.5 7V4.5h5V7M6.5 7l.9 12.2a1.5 1.5 0 0 0 1.5 1.3h6.2a1.5 1.5 0 0 0 1.5-1.3L17.5 7" />
      <path d="M10 11v5.5M14 11v5.5" />
    </>
  ),
  search: (
    <>
      <circle cx="10.5" cy="10.5" r="6.5" />
      <path d="M15.5 15.5L20 20" />
    </>
  ),
  folder: <path d="M3.5 7.5a2 2 0 0 1 2-2h4l2 2.2h7a2 2 0 0 1 2 2v7.8a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z" />,
  'folder-plus': (
    <>
      <path d="M3.5 7.5a2 2 0 0 1 2-2h4l2 2.2h7a2 2 0 0 1 2 2v7.8a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z" />
      <path d="M12 11v5M9.5 13.5h5" />
    </>
  ),
  sun: (
    <>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4" />
    </>
  ),
  cloud: <path d="M7 18.5h10.5a4 4 0 0 0 .6-7.95A6 6 0 0 0 6.6 9.6 4.5 4.5 0 0 0 7 18.5z" />,
  file: (
    <>
      <path d="M13.5 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8.5z" />
      <path d="M13.5 3v5.5H19M9 13h6M9 16.5h4" />
    </>
  ),
  camera: (
    <>
      <path d="M4 8.5a2 2 0 0 1 2-2h2l1.5-2h5L16 6.5h2a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z" />
      <circle cx="12" cy="12.6" r="3.4" />
    </>
  ),
  image: (
    <>
      <rect x="3.5" y="4.5" width="17" height="15" rx="2.2" />
      <circle cx="9" cy="9.6" r="1.7" />
      <path d="M4 17.5l4.6-4.6 3.2 3.2 2.6-2.6L20 18.6" />
    </>
  ),
  film: (
    <>
      <rect x="3.5" y="5" width="17" height="14" rx="2.2" />
      <path d="M7.5 5v14M16.5 5v14M3.5 9.5h4M3.5 14.5h4M16.5 9.5h4M16.5 14.5h4" />
    </>
  ),
  save: (
    <>
      <path d="M5 4.5h11.5L19.5 7.5V18a1.5 1.5 0 0 1-1.5 1.5H6A1.5 1.5 0 0 1 4.5 18V6A1.5 1.5 0 0 1 6 4.5z" />
      <path d="M8 4.5v4.5h7V4.5M7.5 19.5v-5.5h9v5.5" />
    </>
  ),
  undo: (
    <>
      <path d="M8.5 6.5L4 11l4.5 4.5" />
      <path d="M4.5 11H15a5 5 0 0 1 0 10h-3" />
    </>
  ),
  wand: (
    <>
      <path d="M4.5 19.5l10-10M13 7l1.5-1.5 4 4L17 11" />
      <path d="M18 3v2.5M19.25 4.25h-2.5M6 4v2M7 5H5M20 15v2M21 16h-2" />
    </>
  ),
  download: (
    <>
      <path d="M12 4v11M7.5 10.5L12 15l4.5-4.5" />
      <path d="M4.5 17v1.5A1.5 1.5 0 0 0 6 20h12a1.5 1.5 0 0 0 1.5-1.5V17" />
    </>
  ),
  lock: (
    <>
      <rect x="5" y="10.5" width="14" height="10" rx="2" />
      <path d="M8 10.5V7.5a4 4 0 0 1 8 0v3M12 14.5v2.5" />
    </>
  ),
  chrome: (
    <>
      <circle cx="12" cy="12" r="9" />
      <circle cx="12" cy="12" r="3.6" />
      <path d="M12 8.4h8.2M8.9 13.8l-4.1-7.1M15.1 13.8l-4.1 7.1" />
    </>
  ),
  globe: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M3 12h18M12 3c2.6 2.6 3.8 5.6 3.8 9s-1.2 6.4-3.8 9c-2.6-2.6-3.8-5.6-3.8-9S9.4 5.6 12 3z" />
    </>
  ),
  youtube: (
    <>
      <rect x="2.8" y="5.5" width="18.4" height="13" rx="4" />
      <path d="M10.2 9.3v5.4l4.6-2.7z" />
    </>
  ),
  calculator: (
    <>
      <rect x="5" y="2.8" width="14" height="18.4" rx="2.4" />
      <path d="M8.2 6.3h7.6v3.2H8.2zM8.5 13h.1M12 13h.1M15.5 13h.1M8.5 16.8h.1M12 16.8h.1M15.5 16.8h.1" />
    </>
  ),
  bell: (
    <>
      <path d="M6 16.5V11a6 6 0 0 1 12 0v5.5l1.5 2H4.5z" />
      <path d="M10 20.5a2.2 2.2 0 0 0 4 0" />
    </>
  ),
  alert: (
    <>
      <path d="M10.3 4.2L2.8 17.5A2 2 0 0 0 4.5 20.5h15a2 2 0 0 0 1.7-3L13.7 4.2a2 2 0 0 0-3.4 0z" />
      <path d="M12 9.5v4.5M12 17.2v.3" />
    </>
  ),
  info: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 11v5.5M12 7.8v.3" />
    </>
  ),
  send: <path d="M4 12l16-8-6 16-2.5-6.5zM11.5 13.5L20 4" />,
  'chevron-left': <path d="M15 5l-7 7 7 7" />,
  'chevron-right': <path d="M9 5l7 7-7 7" />,
  'chevron-down': <path d="M5 9l7 7 7-7" />,
  'chevron-up': <path d="M5 15l7-7 7 7" />,
  external: (
    <>
      <path d="M14 4h6v6M20 4l-9 9" />
      <path d="M18 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4" />
    </>
  ),
  clock: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5.2l3.3 2" />
    </>
  ),
  cpu: (
    <>
      <rect x="6" y="6" width="12" height="12" rx="2" />
      <rect x="9.5" y="9.5" width="5" height="5" rx=".8" />
      <path d="M9 2.5V6M15 2.5V6M9 18v3.5M15 18v3.5M2.5 9H6M2.5 15H6M18 9h3.5M18 15h3.5" />
    </>
  ),
  ram: (
    <>
      <rect x="3" y="7" width="18" height="10" rx="1.6" />
      <path d="M7 10.5v3M10.5 10.5v3M14 10.5v3M17.5 10.5v3M6 17v2.5M18 17v2.5" />
    </>
  ),
  disk: (
    <>
      <rect x="3" y="13" width="18" height="7" rx="2" />
      <path d="M5.5 13l2.6-7.2A2 2 0 0 1 10 4.5h4a2 2 0 0 1 1.9 1.3L18.5 13" />
      <path d="M16.5 16.5h.1M13.5 16.5h.1" />
    </>
  ),
  network: <path d="M7 20V5M3.5 8.5L7 5l3.5 3.5M17 4v15M13.5 15.5L17 19l3.5-3.5" />,
  'arrow-up': <path d="M12 19V5M6.5 10.5L12 5l5.5 5.5" />,
  'arrow-down': <path d="M12 5v14M6.5 13.5L12 19l5.5-5.5" />,
  battery: (
    <>
      <rect x="2.5" y="7.5" width="17" height="9" rx="2" />
      <path d="M21.5 10.5v3" />
    </>
  ),
  bolt: <path d="M13 2.5L4.5 13.5H11l-1 8 8.5-11H12z" />,
  sparkles: (
    <>
      <path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z" />
      <path d="M18.5 15.5l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8z" />
    </>
  ),
  code: <path d="M8.5 7L3.5 12l5 5M15.5 7l5 5-5 5M13.5 4.5l-3 15" />,
  compass: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M15.8 8.2l-2.2 5.4-5.4 2.2 2.2-5.4z" />
    </>
  ),
  monitor: (
    <>
      <rect x="3" y="4" width="18" height="12.5" rx="2" />
      <path d="M8.5 20.5h7M12 16.5v4" />
    </>
  ),
  key: (
    <>
      <circle cx="8" cy="15" r="4" />
      <path d="M10.9 12.1L20 3M16.5 6.5l2.5 2.5M14 9l2 2" />
    </>
  ),
  filter: <path d="M4 5h16l-6.2 7.4V19l-3.6 1.6v-8.2z" />,
  history: (
    <>
      <path d="M3.5 12a8.5 8.5 0 1 0 2.6-6.1" />
      <path d="M3.5 4.5v4h4M12 8v4.5l3 1.8" />
    </>
  ),
  eye: (
    <>
      <path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z" />
      <circle cx="12" cy="12" r="3" />
    </>
  ),
  grid: (
    <>
      <circle cx="12" cy="5" r="2" />
      <circle cx="19" cy="12" r="2" />
      <circle cx="12" cy="19" r="2" />
      <circle cx="5" cy="12" r="2" />
      <circle cx="12" cy="12" r="1.2" />
    </>
  ),
  wave: <path d="M2.5 12h2M6.5 8v8M10 5v14M13.5 9v6M17 6.5v11M20.5 10.5v3" />,
  terminal: (
    <>
      <rect x="3" y="4.5" width="18" height="15" rx="2" />
      <path d="M7 9.5l3 2.5-3 2.5M12.5 15h4.5" />
    </>
  ),
  user: (
    <>
      <circle cx="12" cy="8" r="4" />
      <path d="M4.5 20.5c.8-4 3.7-6 7.5-6s6.7 2 7.5 6" />
    </>
  ),
  app: <rect x="4" y="4" width="16" height="16" rx="4" />,
  music: (
    <>
      <path d="M9 18V5.5l11-2V16" />
      <circle cx="6.5" cy="18" r="2.5" />
      <circle cx="17.5" cy="16" r="2.5" />
    </>
  ),
  link: (
    <>
      <path d="M10 14a4.5 4.5 0 0 0 6.4 0l3-3a4.5 4.5 0 0 0-6.4-6.4l-1 1" />
      <path d="M14 10a4.5 4.5 0 0 0-6.4 0l-3 3a4.5 4.5 0 0 0 6.4 6.4l1-1" />
    </>
  ),
  layers: (
    <>
      <path d="M12 3l9 4.8-9 4.8-9-4.8z" />
      <path d="M3 12.2l9 4.8 9-4.8M3 16.4l9 4.8 9-4.8" />
    </>
  ),
  list: <path d="M8 6h12M8 12h12M8 18h12M4 6h.1M4 12h.1M4 18h.1" />,
  dots: <path d="M5 12h.1M12 12h.1M19 12h.1" />,
  zap: <path d="M13 2.5L4.5 13.5H11l-1 8 8.5-11H12z" />,
  shield: <path d="M12 3l7.5 3v5.6c0 4.6-3.2 8.2-7.5 9.4-4.3-1.2-7.5-4.8-7.5-9.4V6L12 3z" />,
  speaker: (
    <>
      <path d="M4 9.5h3.5L12 5.5v13l-4.5-4H4z" />
      <path d="M15.5 9a4.2 4.2 0 0 1 0 6M18 6.5a7.8 7.8 0 0 1 0 11" />
    </>
  ),
  target: (
    <>
      <circle cx="12" cy="12" r="9" />
      <circle cx="12" cy="12" r="5" />
      <circle cx="12" cy="12" r="1.2" />
    </>
  ),
  language: (
    <>
      <path d="M4 5.5h9M8.5 3.5v2M11 5.5c-.8 3.5-3.4 6.4-6.5 7.8M6.5 8.5c1.2 2 3 3.6 5 4.5" />
      <path d="M12.5 20.5l3.8-9 3.8 9M13.8 17.5h5" />
    </>
  ),
  move: <path d="M12 3v18M3 12h18M12 3l-2.5 2.5M12 3l2.5 2.5M12 21l-2.5-2.5M12 21l2.5-2.5M3 12l2.5-2.5M3 12l2.5 2.5M21 12l-2.5-2.5M21 12l-2.5 2.5" />,
};

export type IconName = keyof typeof ICONS;

export const ICON_NAMES = Object.keys(ICONS);

interface IconProps {
  name: string;
  size?: number;
  strokeWidth?: number;
  className?: string;
  style?: CSSProperties;
  title?: string;
}

/** Inline SVG icon. Unknown names fall back to a neutral app glyph. */
export function Icon({ name, size = 18, strokeWidth = 1.7, className, style, title }: IconProps) {
  const glyph = ICONS[name] ?? ICONS.app;
  return (
    <svg
      className={className}
      style={style}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden={title ? undefined : true}
      role={title ? 'img' : undefined}
      focusable="false"
    >
      {title ? <title>{title}</title> : null}
      {glyph}
    </svg>
  );
}

export const AGENT_ICONS: Record<string, string> = {
  research: 'search',
  system: 'monitor',
  development: 'code',
  creative: 'sparkles',
  planner: 'compass',
};
