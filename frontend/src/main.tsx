import { lazy, StrictMode, Suspense } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import './styles.css';

const ChatPreview = lazy(() => import('./ChatPreview'));
const ChatPage = lazy(() => import('./ChatPage'));
const showChatPreview = window.location.pathname === '/chat-preview';
const showChat = window.location.pathname === '/chat';

createRoot(document.getElementById('root')!).render(
  <StrictMode>{showChatPreview || showChat ? <Suspense fallback={<p style={{ padding: 24 }}>正在打开对话…</p>}>{showChatPreview ? <ChatPreview /> : <ChatPage />}</Suspense> : <App />}</StrictMode>,
);
