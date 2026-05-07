import Editor, { type OnMount } from '@monaco-editor/react';
import { Check, X } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import useSWR from 'swr';
import {
  commitFile,
  getSettings,
  getTargetContent,
  saveTargetContent,
  type AppSettings,
} from '../api/client';
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from './ui/dialog';
import { Button } from './ui/button';
import { Input } from './ui/input';
// QS.22: Monaco glue (completion provider, YAML validation, initial pass)
// lives in ./editor/. EditorModal stays a thin dialog + state wrapper.
import { loadComponentList, setEsphomeVersion, setupEsphomeEditor } from './editor/monacoSetup';

type ToastType = 'info' | 'success' | 'error';

interface Props {
  target: string | null;
  onClose: () => void;
  /** #42: called right before onClose when the editor closes via a successful
   *  save (Save or Save & Upgrade). Parent uses this to distinguish a
   *  saved-close from a cancel/dismiss-close — cancelling out of a newly
   *  created device with no save should clean up the leftover file. */
  onSaved?: (target: string) => void;
  onToast: (msg: string, type?: ToastType) => void;
  onValidate?: (target: string) => Promise<{ success: boolean; output: string } | null>;
  onCompile?: (target: string) => void;
  /** AV.6: callback when the user clicks the "History" toolbar button.
   *  Parent opens the per-file HistoryPanel drawer. */
  onOpenHistory?: (target: string) => void;
  /** RC.1: callback when the user clicks the "View rendered" toolbar
   *  button. Parent opens the read-only rendered-config modal. */
  onViewRenderedConfig?: (target: string) => void;
  monacoTheme?: string;
  esphomeVersion?: string | null;
  /**
   * Bug #31: bumped by the parent whenever the on-disk content may have
   * changed under us (history Restore, manual commit from the History
   * panel). The fetch effect watches this so the editor buffer reloads
   * to reflect the restored version instead of staying stuck on the
   * original content loaded when the modal opened.
   */
  reloadNonce?: number;
}

// Track dirty-line decorations (module-level so the callback closure can access it)
let _dirtyDecorationIds: string[] = [];

export function EditorModal({ target, onClose, onSaved, onToast, onValidate, onCompile, onOpenHistory, onViewRenderedConfig, monacoTheme = 'vs-dark', esphomeVersion, reloadNonce = 0 }: Props) {
  const isOpen = target !== null;
  const [content, setContent] = useState('');
  const [, setLoading] = useState(false);
  const editorRef = useRef<Parameters<OnMount>[0] | null>(null);
  const monacoRef = useRef<Parameters<OnMount>[1] | null>(null);
  const savedContentRef = useRef('');
  const [dirtyLineCount, setDirtyLineCount] = useState(0);
  const [showCloseConfirm, setShowCloseConfirm] = useState(false);
  // #26: validation output shown inline below the editor.
  const [validateResult, setValidateResult] = useState<{ success: boolean; output: string } | null>(null);
  const [validating, setValidating] = useState(false);
  // Bug #24 / #25: commit-message dialog. Opens when the user presses
  // Save (auto-commit ON), Save & Upgrade (auto-commit ON), or Save
  // and Commit (auto-commit OFF). The pending kind decides what
  // happens after the save+commit succeeds.
  const [commitMsg, setCommitMsg] = useState('');
  const [commitDialogKind, setCommitDialogKind] = useState<
    null | 'save' | 'save-upgrade' | 'save-commit'
  >(null);
  const [commitBusy, setCommitBusy] = useState(false);

  // Bug #24: live-read the auto-commit setting so the Save path chooses
  // between "prompt for a message" and "just save". Revalidated on
  // focus so flipping the toggle in the Settings drawer takes effect
  // here immediately.
  const { data: settings } = useSWR<AppSettings>('settings', getSettings);
  const autoCommit = settings?.auto_commit_on_save ?? true;
  // Bug #111: hide the History toolbar button when versioning is off —
  // there's nothing to show. The `&& onOpenHistory && ...` conjunction
  // below stays, so a parent that doesn't supply the callback also hides
  // the button.
  const versioningEnabled = settings?.versioning_enabled === 'on';

  // Keep the completion provider's module-level version variable in sync so
  // it always sees the current value despite being registered once outside
  // the component lifecycle.
  useEffect(() => {
    if (esphomeVersion) setEsphomeVersion(esphomeVersion);
  }, [esphomeVersion]);

  // Keep stable refs to callbacks so the fetch effect depends only on [target],
  // not on new function references from each parent re-render (which would
  // re-fetch on every poll cycle and wipe local edits).
  const onCloseRef = useRef(onClose);
  const onToastRef = useRef(onToast);
  useEffect(() => { onCloseRef.current = onClose; }, [onClose]);
  useEffect(() => { onToastRef.current = onToast; }, [onToast]);

  // Stable ref so the Monaco Ctrl+S command (registered once on mount) always
  // calls the current save handler without capturing a stale closure.
  const onSaveClickedRef = useRef<() => void>(() => {});

  // Load content when target changes — intentionally [target, reloadNonce]
  // only so that background polls refreshing the parent do NOT overwrite
  // unsaved edits. Bug #31: reloadNonce is bumped by the parent after a
  // History Restore or manual commit so the buffer reloads to match the
  // new on-disk content.
  useEffect(() => {
    if (!target) return;
    setLoading(true);
    getTargetContent(target)
      .then(c => {
        setContent(c);
        savedContentRef.current = c;
        setLoading(false);
      })
      .catch(err => {
        onToastRef.current('Failed to load file: ' + (err as Error).message, 'error');
        setLoading(false);
        onCloseRef.current();
      });

    // Pre-fetch the component list as soon as the editor opens so completions
    // are available without waiting for the first keypress.
    loadComponentList().catch(() => null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target, reloadNonce]);

  async function updateDirtyDecorations(editor: Parameters<OnMount>[0]) {
    const model = editor.getModel();
    if (!model || !monacoRef.current) return;
    const monaco = monacoRef.current;

    const currentValue = model.getValue();
    const savedValue = savedContentRef.current;

    if (currentValue === savedValue) {
      _dirtyDecorationIds = editor.deltaDecorations(_dirtyDecorationIds, []);
      setDirtyLineCount(0);
      return;
    }

    // Use Monaco's built-in diff computation via the editor worker service.
    // Create a temporary model for the saved content so Monaco can diff them.
    const savedModel = monaco.editor.createModel(savedValue, 'yaml');
    try {
      let changes: { modifiedStartLineNumber: number; modifiedEndLineNumber: number }[] = [];
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const editorWorker = (monaco.editor as any) /* ALLOW_ANY: monaco internal */.getEditorWorkerService?.();
      if (editorWorker?.computeDiff) {
        const diff = await editorWorker.computeDiff(savedModel.uri, model.uri, false, 100000);
        changes = (diff?.changes ?? []).map((c: { modified: { startLineNumber: number; endLineNumberExclusive: number } }) => ({
          modifiedStartLineNumber: c.modified.startLineNumber,
          modifiedEndLineNumber: c.modified.endLineNumberExclusive - 1,
        }));
      }

      // Fallback: common prefix/suffix approach if worker API unavailable
      if (changes.length === 0 && currentValue !== savedValue) {
        const origLines = savedValue.split('\n');
        const modLines = currentValue.split('\n');
        let prefixLen = 0;
        while (prefixLen < origLines.length && prefixLen < modLines.length && origLines[prefixLen] === modLines[prefixLen]) prefixLen++;
        let suffixLen = 0;
        while (suffixLen < origLines.length - prefixLen && suffixLen < modLines.length - prefixLen && origLines[origLines.length - 1 - suffixLen] === modLines[modLines.length - 1 - suffixLen]) suffixLen++;
        if (prefixLen < modLines.length - suffixLen) {
          changes = [{ modifiedStartLineNumber: prefixLen + 1, modifiedEndLineNumber: modLines.length - suffixLen }];
        }
      }

      const decorations: import('monaco-editor').editor.IModelDeltaDecoration[] = [];
      for (const change of changes) {
        for (let line = change.modifiedStartLineNumber; line <= change.modifiedEndLineNumber; line++) {
          decorations.push({
            range: { startLineNumber: line, startColumn: 1, endLineNumber: line, endColumn: 1 },
            options: { isWholeLine: true, className: 'dirty-line', glyphMarginClassName: 'dirty-glyph' },
          });
        }
      }
      _dirtyDecorationIds = editor.deltaDecorations(_dirtyDecorationIds, decorations);
      setDirtyLineCount(decorations.length);
    } finally {
      savedModel.dispose();
    }
  }

  function handleEditorDidMount(
    editor: Parameters<OnMount>[0],
    monaco: Parameters<OnMount>[1],
  ) {
    editorRef.current = editor;
    monacoRef.current = monaco;
    _dirtyDecorationIds = [];

    // Completion + validation + initial pass are all handled in
    // ./editor/monacoSetup. We still need our own content-change listener
    // to update the dirty-line decorations on the left gutter.
    setupEsphomeEditor(editor, monaco);
    editor.onDidChangeModelContent(() => {
      updateDirtyDecorations(editor).catch(() => {});
    });

    // Bug #135: intercept Ctrl+S / Cmd+S inside Monaco so the browser's
    // "Save page as HTML" dialog never fires. KeyMod.CtrlCmd resolves to
    // Cmd on Mac and Ctrl on Win/Linux — one registration covers both.
    // The command reads from onSaveClickedRef so it always invokes the
    // current save handler even though it was registered once at mount time.
    editor.addCommand(
      monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS,
      () => { onSaveClickedRef.current(); },
    );
  }

  // Bug #24 entry point for the "Save" button. Auto-commit ON: prompt
  // for a commit message first, then run save+commit. Auto-commit OFF:
  // just save (the explicit "Save and Commit" button handles the
  // commit path for that case).
  // Bug #135: also invoked via the Monaco Ctrl+S command (see handleEditorDidMount).
  function onSaveClicked() {
    if (autoCommit) {
      setCommitMsg('');
      setCommitDialogKind('save');
    } else {
      void handleSave();
    }
  }

  // Keep the ref in sync so the Monaco command always calls the current version.
  // Placed after the function definition so the ref update sees it immediately.
  onSaveClickedRef.current = onSaveClicked;

  function onSaveAndUpgradeClicked() {
    if (autoCommit) {
      setCommitMsg('');
      setCommitDialogKind('save-upgrade');
    } else {
      void handleSaveAndUpgrade();
    }
  }

  // Bug #25: only rendered when auto-commit is OFF — explicit manual
  // commit path. Always opens the commit-message dialog.
  function onSaveAndCommitClicked() {
    setCommitMsg('');
    setCommitDialogKind('save-commit');
  }

  // Bug #136: save no longer closes the modal — user closes explicitly via the
  // Close button. handleSave stays open; Save & Upgrade and Save & Commit still
  // call onClose() directly because those flows are inherently navigate-away.
  async function handleSave(userMessage?: string) {
    if (!editorRef.current || !target) return false;
    const value = editorRef.current.getValue();
    try {
      const { renamedTo } = await saveTargetContent(target, value, userMessage);
      const finalTarget = renamedTo ?? target;
      savedContentRef.current = value;
      if (editorRef.current) updateDirtyDecorations(editorRef.current).catch(() => {});
      onToast('Saved ' + finalTarget, 'success');
      onSaved?.(target);
      return true;
    } catch (err) {
      onToast('Save failed: ' + (err as Error).message, 'error');
      return false;
    }
  }

  async function handleSaveAndUpgrade(userMessage?: string) {
    if (!editorRef.current || !target) return false;
    const value = editorRef.current.getValue();
    try {
      const { renamedTo } = await saveTargetContent(target, value, userMessage);
      const finalTarget = renamedTo ?? target;
      savedContentRef.current = value;
      onToast('Saved ' + finalTarget, 'success');
      onSaved?.(target);
      onCompile?.(finalTarget);
      onClose();
      return true;
    } catch (err) {
      onToast('Save failed: ' + (err as Error).message, 'error');
      return false;
    }
  }

  // Bug #25: save (no server-side auto-commit because it's off) then
  // call the explicit ``/files/{f}/commit`` endpoint with the user's
  // message. Runs from the commit-message dialog's "Save and commit"
  // button when auto-commit is off.
  async function handleSaveAndCommit(userMessage: string) {
    if (!editorRef.current || !target) return false;
    const value = editorRef.current.getValue();
    try {
      const { renamedTo } = await saveTargetContent(target, value);
      const finalTarget = renamedTo ?? target;
      savedContentRef.current = value;
      onSaved?.(target);
      try {
        const result = await commitFile(finalTarget, userMessage);
        if (result.committed) {
          onToast(`Committed ${finalTarget} (${result.short_hash})`, 'success');
        } else {
          onToast(`Saved ${finalTarget} (no changes to commit)`, 'info');
        }
      } catch (err) {
        onToast('Commit failed: ' + (err as Error).message, 'error');
        return false;
      }
      onClose();
      return true;
    } catch (err) {
      onToast('Save failed: ' + (err as Error).message, 'error');
      return false;
    }
  }

  async function confirmCommitDialog() {
    const kind = commitDialogKind;
    if (!kind) return;
    setCommitBusy(true);
    try {
      if (kind === 'save') {
        await handleSave(commitMsg);
      } else if (kind === 'save-upgrade') {
        await handleSaveAndUpgrade(commitMsg);
      } else if (kind === 'save-commit') {
        await handleSaveAndCommit(commitMsg);
      }
    } finally {
      setCommitBusy(false);
      setCommitDialogKind(null);
    }
  }

  if (!isOpen) return null;

  return (
    <Dialog open onOpenChange={(open) => {
      if (!open) {
        if (dirtyLineCount > 0) { setShowCloseConfirm(true); return; }
        onClose();
      }
    }}>
      <DialogContent className="dialog-xl" style={{ background: monacoTheme === 'vs' ? '#ffffff' : '#1e1e1e', border: monacoTheme === 'vs' ? '1px solid var(--border)' : '1px solid #3c3c3c' }}>
        <div className="editor-header">
          <h3>{(target || '').replace(/^\.pending\./, '')}</h3>
          <Button size="sm" onClick={onSaveClicked}>Save</Button>
          {!autoCommit && (
            <Button
              variant="secondary"
              size="sm"
              onClick={onSaveAndCommitClicked}
              title="Save and create a git commit with a custom message. Auto-commit is off."
            >
              Save &amp; Commit
            </Button>
          )}
          {onCompile && target && target !== 'secrets.yaml' && (
            <Button
              variant="success"
              size="sm"
              onClick={onSaveAndUpgradeClicked}
              title="Save and trigger firmware compile + OTA"
            >
              Save &amp; Upgrade
            </Button>
          )}
          {onOpenHistory && versioningEnabled && target && !target.startsWith('.pending.') && (
            <Button
              variant="secondary"
              size="sm"
              onClick={() => onOpenHistory(target)}
              title="View version history + diff for this file"
            >
              History
            </Button>
          )}
          {/* RC.1: open the read-only "what will ESPHome compile?" view.
              Only offered for real device YAMLs — secrets.yaml has no
              device config to render, and the .pending. stub files
              aren't on disk yet. */}
          {onViewRenderedConfig && target && target !== 'secrets.yaml' && !target.startsWith('.pending.') && (
            <Button
              variant="secondary"
              size="sm"
              onClick={() => onViewRenderedConfig(target)}
              title="Show the YAML as ESPHome will compile it (substitutions / packages / !secret resolved)"
            >
              View rendered
            </Button>
          )}
          {onValidate && target && target !== 'secrets.yaml' && (
            <Button
              variant="secondary"
              size="sm"
              disabled={validating}
              onClick={async () => {
                if (!editorRef.current || !target) return;
                const value = editorRef.current.getValue();
                try {
                  await saveTargetContent(target, value);
                  savedContentRef.current = value;
                  updateDirtyDecorations(editorRef.current).catch(() => {});
                } catch (err) {
                  onToast('Save failed: ' + (err as Error).message, 'error');
                  return;
                }
                setValidating(true);
                setValidateResult(null);
                const result = await onValidate(target);
                setValidating(false);
                if (result) setValidateResult(result);
              }}
              title="Save and validate config via esphome config (2-5s)"
            >
              {validating ? 'Validating…' : 'Validate'}
            </Button>
          )}
        </div>
        <div className="monaco-container">
          <Editor
            height="100%"
            defaultLanguage="yaml"
            value={content}
            theme={monacoTheme}
            options={{
              fontSize: 13,
              lineNumbers: 'on',
              minimap: { enabled: false },
              wordWrap: 'on',
              scrollBeyondLastLine: false,
              automaticLayout: true,
              tabSize: 2,
              insertSpaces: true,
              quickSuggestions: { other: true, strings: true, comments: false },
              suggestOnTriggerCharacters: true,
              wordBasedSuggestions: 'off',
              acceptSuggestionOnCommitCharacter: true,
              hover: { enabled: true },
              glyphMargin: true,
            }}
            onMount={handleEditorDidMount}
          />
        </div>
        {/* #26: validation output panel — shows the raw esphome config output */}
        {validateResult && (
          <div
            className="border-t px-3 py-2 font-mono text-xs overflow-auto"
            style={{
              maxHeight: 180,
              background: validateResult.success ? 'var(--surface)' : 'rgba(239,68,68,0.08)',
              borderColor: validateResult.success ? 'var(--border)' : 'var(--destructive)',
              color: validateResult.success ? 'var(--success)' : 'var(--destructive)',
            }}
          >
            <div className="flex items-center justify-between mb-1">
              <span className="inline-flex items-center gap-1 font-semibold text-[11px] uppercase tracking-wide">
                {validateResult.success
                  ? (<><Check className="size-3.5" aria-hidden="true" /> Validation passed</>)
                  : (<><X className="size-3.5" aria-hidden="true" /> Validation failed</>)}
              </span>
              <button
                className="text-[var(--text-muted)] text-[10px] cursor-pointer hover:text-[var(--text)]"
                onClick={() => setValidateResult(null)}
              >
                dismiss
              </button>
            </div>
            <pre className="whitespace-pre-wrap break-words m-0" style={{ color: 'var(--text)' }}>{validateResult.output}</pre>
          </div>
        )}
        {/* Bug #136: footer is always rendered so Close is always reachable.
            The dirty-line count is shown inline when there are changes. */}
        <div className="editor-footer">
          {dirtyLineCount > 0 && !validateResult && (
            <span>{dirtyLineCount} line{dirtyLineCount !== 1 ? 's' : ''} changed</span>
          )}
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              if (dirtyLineCount > 0) { setShowCloseConfirm(true); return; }
              onClose();
            }}
          >
            Close
          </Button>
        </div>
      </DialogContent>
      {showCloseConfirm && (
        <Dialog open onOpenChange={(open) => { if (!open) setShowCloseConfirm(false); }}>
          <DialogContent style={{ zIndex: 600 }}>
            <DialogHeader>
              <DialogTitle>Unsaved Changes</DialogTitle>
            </DialogHeader>
            <div style={{ padding: 16 }}>
              <p>You have {dirtyLineCount} unsaved line{dirtyLineCount !== 1 ? 's' : ''}. Close without saving?</p>
            </div>
            <DialogFooter>
              <Button variant="secondary" size="sm" onClick={() => setShowCloseConfirm(false)}>Cancel</Button>
              <Button variant="destructive" size="sm" onClick={() => { setShowCloseConfirm(false); onClose(); }}>Discard Changes</Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
      {/* Bug #24 / #25: commit-message prompt. Shared between Save,
          Save & Upgrade, and Save & Commit — `commitDialogKind` drives
          the copy and which post-save action runs on confirm. */}
      {commitDialogKind && (
        <Dialog open onOpenChange={(open) => { if (!open && !commitBusy) setCommitDialogKind(null); }}>
          <DialogContent style={{ zIndex: 600 }}>
            <DialogHeader>
              <DialogTitle>
                {commitDialogKind === 'save-upgrade'
                  ? 'Commit message for save & upgrade'
                  : commitDialogKind === 'save-commit'
                    ? 'Commit message'
                    : 'Commit message for save'}
              </DialogTitle>
            </DialogHeader>
            <div style={{ padding: 16, display: 'flex', flexDirection: 'column', gap: 12 }}>
              <p className="text-sm text-[var(--text-muted)]">
                {commitDialogKind === 'save-commit'
                  ? 'Saving and creating a git commit for this file. Leave blank to use the default message.'
                  : 'This save will create a git commit. Leave blank to use the default message.'}
              </p>
              <Input
                autoFocus
                placeholder={`save: ${(target || '').replace(/^\.pending\./, '')}`}
                value={commitMsg}
                onChange={e => setCommitMsg(e.target.value)}
                onKeyDown={e => {
                  if (e.key === 'Enter' && !commitBusy) {
                    e.preventDefault();
                    void confirmCommitDialog();
                  } else if (e.key === 'Escape' && !commitBusy) {
                    e.preventDefault();
                    setCommitDialogKind(null);
                  }
                }}
                maxLength={200}
                disabled={commitBusy}
              />
            </div>
            <DialogFooter>
              <Button
                variant="secondary"
                size="sm"
                onClick={() => setCommitDialogKind(null)}
                disabled={commitBusy}
              >
                Cancel
              </Button>
              <Button
                size="sm"
                onClick={() => { void confirmCommitDialog(); }}
                disabled={commitBusy}
              >
                {commitBusy
                  ? 'Saving…'
                  : commitDialogKind === 'save-upgrade'
                    ? 'Save, commit & upgrade'
                    : commitDialogKind === 'save-commit'
                      ? 'Save and commit'
                      : 'Save and commit'}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
    </Dialog>
  );
}
