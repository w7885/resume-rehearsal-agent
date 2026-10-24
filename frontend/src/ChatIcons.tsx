import type { ReactNode } from 'react';

type IconName = 'plus' | 'chat' | 'panel' | 'attach' | 'send' | 'close' | 'download' | 'copy' | 'edit' | 'menu' | 'chevron' | 'more' | 'trash';
export function Icon({ name, size = 20 }: { name: IconName; size?: number }) {
  const paths: Record<IconName, ReactNode> = {
    plus: <path d="M12 5v14M5 12h14" />,
    chat: <path d="M20 11.5a8 8 0 0 1-8 8H5l-3 2v-10a9 9 0 0 1 18 0Z" />,
    panel: <><rect x="3" y="4" width="18" height="16" rx="3" /><path d="M15 4v16" /></>,
    attach: <path d="m8 13 6.7-6.7a3.5 3.5 0 0 1 5 5L10 21a5 5 0 0 1-7-7L13 4a2 2 0 0 1 3 3L6.5 16.5" />,
    send: <path d="M12 19V5m-6 6 6-6 6 6" />,
    close: <path d="m6 6 12 12M6 18 18 6" />,
    download: <><path d="M12 3v12m-5-5 5 5 5-5M4 16v4h16v-4" /></>,
    copy: <><rect x="8" y="8" width="12" height="13" rx="2" /><path d="M16 8V3H3v13h5" /></>,
    edit: <><path d="m15 4 5 5M4 20l4-1L21 6a2 2 0 0 0-5-5L3 14l-1 6Z" /></>,
    menu: <path d="M4 6h16M4 12h16M4 18h16" />,
    chevron: <path d="m9 5 7 7-7 7" />,
    more: <><circle cx="5" cy="12" r="1" /><circle cx="12" cy="12" r="1" /><circle cx="19" cy="12" r="1" /></>,
    trash: <><path d="M3 6h18M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7M14 10v7" /></>,
  };
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name]}</svg>;
}
