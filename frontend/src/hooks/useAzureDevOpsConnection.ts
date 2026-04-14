'use client';

import { useCallback, useEffect, useState } from 'react';
import { apiClient } from '@/lib/api-client';

export interface AzureDevOpsConnectionState {
  /** True when the user (or server admin fallback) has a PAT configured */
  connected: boolean;
  /** True while the initial status check is in flight */
  checking: boolean;
  /** True while a save or delete request is in flight */
  saving: boolean;
  /** Last error from a save/delete operation, cleared on next attempt */
  error: string | null;
  /** True if the connection comes from a personal PAT rather than the server fallback */
  hasPersonalPat: boolean;
  /** Save a new PAT. Returns true on success. */
  savePat: (pat: string) => Promise<boolean>;
  /** Remove the personal PAT. Falls back to server PAT if one is configured. */
  disconnect: () => Promise<void>;
  /** Re-run the status check (e.g. after a widget mounts) */
  refresh: () => void;
}

const CACHE_KEY = 'ado_connection_status';
const CACHE_TTL_MS = 60_000; // 1 minute

interface CacheEntry {
  connected: boolean;
  hasPersonalPat: boolean;
  ts: number;
}

function readCache(): CacheEntry | null {
  try {
    const raw = sessionStorage.getItem(CACHE_KEY);
    if (!raw) return null;
    const entry: CacheEntry = JSON.parse(raw);
    if (Date.now() - entry.ts > CACHE_TTL_MS) return null;
    return entry;
  } catch {
    return null;
  }
}

function writeCache(connected: boolean, hasPersonalPat: boolean) {
  try {
    const entry: CacheEntry = { connected, hasPersonalPat, ts: Date.now() };
    sessionStorage.setItem(CACHE_KEY, JSON.stringify(entry));
  } catch {
    // ignore
  }
}

function clearCache() {
  try {
    sessionStorage.removeItem(CACHE_KEY);
  } catch {
    // ignore
  }
}

/**
 * Shared hook that manages per-user Azure DevOps PAT state.
 *
 * Uses sessionStorage to cache the status check for 60 s so that multiple
 * widgets on the same page each independently calling this hook do NOT fire
 * N simultaneous requests.
 */
export function useAzureDevOpsConnection(): AzureDevOpsConnectionState {
  const cached = readCache();
  const [connected, setConnected] = useState(cached?.connected ?? false);
  const [hasPersonalPat, setHasPersonalPat] = useState(cached?.hasPersonalPat ?? false);
  const [checking, setChecking] = useState(!cached);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const checkStatus = useCallback(async () => {
    const live = readCache();
    if (live) {
      setConnected(live.connected);
      setHasPersonalPat(live.hasPersonalPat);
      setChecking(false);
      return;
    }
    setChecking(true);
    try {
      const res = await apiClient.getAzureDevOpsPatStatus();
      const c = res.data.configured;
      const h = res.data.has_personal_pat;
      setConnected(c);
      setHasPersonalPat(h);
      writeCache(c, h);
    } catch {
      setConnected(false);
      setHasPersonalPat(false);
    } finally {
      setChecking(false);
    }
  }, []);

  useEffect(() => {
    checkStatus();
  }, [checkStatus]);

  const savePat = useCallback(async (pat: string): Promise<boolean> => {
    setError(null);
    setSaving(true);
    try {
      await apiClient.saveAzureDevOpsPat(pat.trim());
      clearCache();
      writeCache(true, true);
      setConnected(true);
      setHasPersonalPat(true);
      return true;
    } catch (err: any) {
      const msg =
        err?.response?.data?.detail ??
        err?.message ??
        'Failed to save PAT';
      setError(msg);
      return false;
    } finally {
      setSaving(false);
    }
  }, []);

  const disconnect = useCallback(async () => {
    setSaving(true);
    setError(null);
    try {
      await apiClient.deleteAzureDevOpsPat();
      clearCache();
      // After removing personal PAT, re-check to see if server fallback still works
      await checkStatus();
    } catch (err: any) {
      setError(err?.response?.data?.detail ?? err?.message ?? 'Failed to disconnect');
    } finally {
      setSaving(false);
    }
  }, [checkStatus]);

  return { connected, checking, saving, error, hasPersonalPat, savePat, disconnect, refresh: checkStatus };
}
