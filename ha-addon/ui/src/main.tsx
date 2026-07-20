import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
// CF.1: wire @monaco-editor/react to the locally-bundled `monaco-editor`
// package BEFORE any editor component mounts. Must happen before
// importing App (which transitively imports the editor modal). Dropping
// this import reverts to the jsDelivr-CDN loader + reintroduces the
// CSP origin we're trying to remove.
import './monaco-local'
// I18N.1: initialise i18next before App mounts so any t() call during
// the initial render resolves against the catalog rather than rendering
// the bare key. The Provider wires the singleton into React context;
// components consume it via the `useTranslation` hook.
import { I18nextProvider } from 'react-i18next'
import i18n from './i18n'
import App from './App.tsx'
import { ErrorBoundary } from './components/ErrorBoundary'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ErrorBoundary>
      <I18nextProvider i18n={i18n}>
        <App />
      </I18nextProvider>
    </ErrorBoundary>
  </StrictMode>,
)
