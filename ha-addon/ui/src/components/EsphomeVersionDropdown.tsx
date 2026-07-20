import { useMemo, useState } from 'react';
import { RotateCw } from 'lucide-react';
import type { EsphomeVersions } from '../types';
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
} from './ui/dropdown-menu';

interface Props {
  versions: EsphomeVersions;
  onSelect: (version: string) => void;
  onRefresh?: () => void;
}

function isBeta(v: string): boolean {
  return /\d(a|b|rc|dev)\d/i.test(v);
}

// #131 sub-bullet 4: floor for the "Installable only" filter.
// ESPHome 2023.7.0 is the first release to officially support Python
// 3.11+, which is what the add-on / server image runs. Anything older
// likely fails at ``pip install esphome==X`` on the runtime Python and
// the user wastes a few minutes finding out. The filter hides those
// from the dropdown by default; users who genuinely want one of the
// older builds can untick the box to see the full PyPI list.
const INSTALLABLE_FLOOR = '2023.7.0';

function compareEsphomeVersion(a: string, b: string): number {
  // Strip the ``a|b|rc|devN`` suffix when comparing so "2024.6.0" and
  // "2024.6.0b1" sort adjacently — sufficient for the floor check; the
  // dropdown's display order is already PyPI's.
  const parse = (v: string): number[] => {
    const base = v.split(/[a-z]/i)[0];
    return base.split('.').map(p => parseInt(p, 10) || 0);
  };
  const av = parse(a);
  const bv = parse(b);
  const len = Math.max(av.length, bv.length);
  for (let i = 0; i < len; i++) {
    const diff = (av[i] ?? 0) - (bv[i] ?? 0);
    if (diff !== 0) return diff;
  }
  return 0;
}

function isInstallable(v: string): boolean {
  return compareEsphomeVersion(v, INSTALLABLE_FLOOR) >= 0;
}

export function EsphomeVersionDropdown({ versions, onSelect, onRefresh }: Props) {
  const sel = versions.selected || '?';
  const [search, setSearch] = useState('');
  const [showBetas, setShowBetas] = useState(false);
  // #131 sub-bullet 4: default ON; uncheck to see legacy versions.
  const [installableOnly, setInstallableOnly] = useState(true);

  const filtered = useMemo(() => {
    let list = versions.available;
    if (!showBetas) list = list.filter(v => !isBeta(v));
    if (installableOnly) list = list.filter(isInstallable);
    if (search) {
      const lc = search.toLowerCase();
      list = list.filter(v => v.toLowerCase().includes(lc));
    }
    return list;
  }, [versions.available, showBetas, installableOnly, search]);

  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
    <DropdownMenu>
      <DropdownMenuTrigger className="rounded-full border border-[var(--border)] bg-[var(--surface2)] px-2 py-0.5 text-[11px] text-[var(--text-muted)] whitespace-nowrap" title="Click to change ESPHome version" style={{ cursor: 'pointer' }}>
        ESPHome {sel} <svg width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" style={{ display: 'inline', verticalAlign: 'middle' }}><path d="m6 9 6 6 6-6"/></svg>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" style={{ maxHeight: 400, overflowY: 'auto' }}>
        <DropdownMenuGroup>
          <DropdownMenuLabel>ESPHome Version</DropdownMenuLabel>
          <DropdownMenuSeparator />
          <div className="px-2 pb-1.5 flex flex-col gap-1.5">
            <input
              type="text"
              value={search}
              onChange={e => setSearch(e.target.value)}
              placeholder="Search versions..."
              className="w-full rounded border border-[var(--border)] bg-[var(--surface)] px-2 py-1 text-[12px] text-[var(--text)] outline-none placeholder:text-[var(--text-muted)] focus:border-[var(--accent)]"
              onClick={e => e.stopPropagation()}
              onKeyDown={e => e.stopPropagation()}
            />
            <label className="flex items-center gap-1.5 text-[11px] text-[var(--text-muted)] cursor-pointer">
              <input type="checkbox" checked={showBetas} onChange={e => setShowBetas(e.target.checked)} />
              Show betas
            </label>
            <label
              className="flex items-center gap-1.5 text-[11px] text-[var(--text-muted)] cursor-pointer"
              title={`Hides ESPHome versions older than ${INSTALLABLE_FLOOR} that won't pip install on the current Python runtime.`}
            >
              <input type="checkbox" checked={installableOnly} onChange={e => setInstallableOnly(e.target.checked)} />
              Installable only
            </label>
          </div>
          <DropdownMenuSeparator />
          {filtered.length === 0 ? (
            <DropdownMenuItem disabled>
              {versions.available.length === 0 ? 'Loading...' : 'No matches'}
            </DropdownMenuItem>
          ) : (
            filtered.map(v => (
              <DropdownMenuItem
                key={v}
                onClick={() => onSelect(v)}
                style={v === versions.selected ? { color: 'var(--accent)', fontWeight: 600 } : undefined}
              >
                {v}
                {v === versions.detected && (
                  <span style={{ fontSize: 10, color: 'var(--text-muted)', marginLeft: 8 }}>(installed)</span>
                )}
              </DropdownMenuItem>
            ))
          )}
        </DropdownMenuGroup>
      </DropdownMenuContent>
    </DropdownMenu>
    {onRefresh && (
      <button
        className="inline-flex items-center justify-center rounded-full border border-[var(--border)] bg-[var(--surface2)] w-[22px] h-[22px] text-[var(--text-muted)] cursor-pointer hover:bg-[var(--border)]"
        title="Refresh available ESPHome versions from PyPI"
        aria-label="Refresh ESPHome versions"
        onClick={onRefresh}
      >
        <RotateCw className="size-3" />
      </button>
    )}
    </span>
  );
}
