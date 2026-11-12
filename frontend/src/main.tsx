import { lazy, StrictMode, Suspense } from 'react';
import { createRoot } from 'react-dom/client';
import './styles.css';
import AuthGate from './AuthGate';

const ChatPage = lazy(() => import('./ChatPage'));

createRoot(document.getElementById('root')!).render(
  <StrictMode><AuthGate>{onOpenSettings => (
    <Suspense fallback={<p style={{ padding: 24 }}>正在打开对话…</p>}>
      <ChatPage onOpenSettings={onOpenSettings} />
    </Suspense>
  )}</AuthGate></StrictMode>,
);
