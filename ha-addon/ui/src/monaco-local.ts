/**
 * CF.1 — point @monaco-editor/react at the locally-bundled
 * ``monaco-editor`` package so we can drop ``cdn.jsdelivr.net`` from
 * the CSP and ship a self-contained UI.
 *
 * By default, ``@monaco-editor/react``'s loader fetches Monaco's
 * runtime + worker scripts from jsDelivr at first editor mount. That
 * forced the addon's CSP to allow ``https://cdn.jsdelivr.net`` across
 * script-src / style-src / font-src / connect-src — a long-standing
 * supply-chain wart, and a reliability hazard (the editor just breaks
 * when jsDelivr is down or blocked by the user's network).
 *
 * The fix: import ``monaco-editor`` normally (Vite bundles it into the
 * app's chunks), wire up ``self.MonacoEnvironment`` so Monaco spawns
 * its own Web Worker from that same bundle, and call
 * ``loader.config({ monaco })`` so ``@monaco-editor/react`` uses the
 * local module instead of fetching from CDN.
 *
 * YAML specifically doesn't ship a dedicated language worker — the
 * standard ``editor.worker`` handles its tokenisation — so one worker
 * constructor covers every label Monaco asks for.
 */

// Tree-shaken Monaco import: only the core editor + YAML tokenisation
// ship to the browser. The full ``monaco-editor`` barrel pulls in every
// basic language (TypeScript, JSON, Go, Solidity, Rust, Dockerfile…) —
// ~3.6 MB raw on disk — for zero benefit here since ESPHome configs are
// always YAML. ``editor.api`` is the one-file core entry; the YAML
// register side-effect import adds YAML to Monaco's language registry
// so ``defaultLanguage="yaml"`` works on the Editor component.
//
// DEP.2 — paths changed shape in monaco 0.56. Its exports map is now
// ``"./*": "./esm/vs/*.js"``, i.e. the ``esm/vs/`` prefix is supplied by
// the map rather than written in the specifier; keeping the old
// ``monaco-editor/esm/vs/…`` form resolves to ``esm/vs/esm/vs/…`` and
// fails (TS2307). 0.56 also reorganised the language definitions:
// ``basic-languages/yaml/yaml.contribution.js`` is now
// ``languages/definitions/yaml/register.js``. The explicit ``.js``
// extension still matters — it keeps TS under ``moduleResolution:
// bundler`` finding the sibling ``.d.ts`` while Vite bundles only what
// is imported, instead of falling through to the barrel entry that
// re-exports every bundled language.
import * as monaco from 'monaco-editor/editor/editor.api.js';
import 'monaco-editor/features/register.all.js';
import 'monaco-editor/languages/definitions/yaml/register.js';
import EditorWorker from 'monaco-editor/editor/editor.worker.js?worker';
import { loader } from '@monaco-editor/react';

// MonacoEnvironment must be set on `self` BEFORE Monaco is used.
// Vite's `?worker` import returns a Worker constructor that wraps the
// bundled worker script — same-origin, CSP-friendly, and no CDN fetch.
// Cast to unknown then the partial type Monaco expects to satisfy
// the "global augmentation" pattern without dragging in @types/monaco-*.
(self as unknown as { MonacoEnvironment: unknown }).MonacoEnvironment = {
  // Monaco passes a (_moduleId, label) pair — we return the same worker
  // for every label because YAML doesn't have a language-specific worker
  // and the basic editor worker covers tokenisation + diff / search.
  getWorker(_moduleId: string, _label: string): Worker {
    return new EditorWorker();
  },
};

loader.config({ monaco });
