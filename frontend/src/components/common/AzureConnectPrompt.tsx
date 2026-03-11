'use client';

import { useState } from 'react';
import { KeyRound, Unlink, ExternalLink, Loader2 } from 'lucide-react';
import type { AzureDevOpsConnectionState } from '@/hooks/useAzureDevOpsConnection';

interface AzureConnectPromptProps {
  connection: AzureDevOpsConnectionState;
  /** Accent colour for the widget header icon, e.g. "bg-blue-600" */
  accentClass?: string;
}

/**
 * Reusable panel shown inside an ADO widget when the user is not connected,
 * or in compact form when connected so they can manage their PAT.
 */
export function AzureConnectPrompt({ connection, accentClass = 'bg-blue-600' }: AzureConnectPromptProps) {
  const { connected, checking, saving, error, hasPersonalPat, savePat, disconnect } = connection;
  const [showForm, setShowForm] = useState(false);
  const [pat, setPat] = useState('');

  const handleSave = async () => {
    const ok = await savePat(pat);
    if (ok) {
      setPat('');
      setShowForm(false);
    }
  };

  if (checking) {
    return (
      <div className="flex items-center justify-center py-4 text-gray-500 dark:text-gray-400 text-sm gap-2">
        <Loader2 className="w-4 h-4 animate-spin" />
        <span>Checking Azure DevOps connection…</span>
      </div>
    );
  }

  if (connected && !showForm) {
    if (!hasPersonalPat) {
      // Connected via the shared server PAT – show a subtle prompt to add a personal one
      return (
        <div className="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400 mt-3">
          <KeyRound className="w-3 h-3 text-yellow-400" />
          <span>Using shared token.</span>
          <button
            onClick={() => setShowForm(true)}
            className="ml-auto text-blue-500 hover:underline transition"
          >
            Add your own PAT
          </button>
        </div>
      );
    }
    return (
      <div className="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400 mt-3">
        <KeyRound className="w-3 h-3 text-green-500" />
        <span>Connected with your personal PAT</span>
        <button
          onClick={disconnect}
          disabled={saving}
          className="ml-auto flex items-center gap-1 text-red-500 hover:text-red-700 transition disabled:opacity-50"
        >
          {saving ? <Loader2 className="w-3 h-3 animate-spin" /> : <Unlink className="w-3 h-3" />}
          Disconnect
        </button>
      </div>
    );
  }

  if (!connected && !showForm) {
    return (
      <div className="mt-4 p-4 rounded-lg border border-dashed border-gray-300 dark:border-gray-600 text-center space-y-3">
        <p className="text-sm text-gray-600 dark:text-gray-400">
          Connect your Azure DevOps account to view live data.
        </p>
        <a
          href="https://learn.microsoft.com/en-us/azure/devops/organizations/accounts/use-personal-access-tokens-to-authenticate"
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-1 text-xs text-blue-500 hover:underline"
        >
          <ExternalLink className="w-3 h-3" /> How to create a PAT
        </a>
        <button
          onClick={() => setShowForm(true)}
          className={`w-full py-2 rounded-md text-white text-sm font-medium transition ${accentClass} hover:opacity-90`}
        >
          Connect via Personal Access Token
        </button>
      </div>
    );
  }

  return (
    <div className="mt-4 space-y-3">
      {error && (
        <p className="text-xs text-red-500 bg-red-50 dark:bg-red-900/20 rounded p-2">{error}</p>
      )}
      <input
        type="password"
        placeholder="Paste your Azure DevOps PAT here"
        value={pat}
        onChange={e => setPat(e.target.value)}
        className="w-full px-3 py-2 rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-sm text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-2 focus:ring-blue-500"
      />
      <div className="flex gap-2">
        <button
          onClick={handleSave}
          disabled={saving || !pat.trim()}
          className={`flex-1 py-2 rounded-md text-white text-sm font-medium transition ${accentClass} hover:opacity-90 disabled:opacity-50`}
        >
          {saving ? <Loader2 className="w-4 h-4 animate-spin mx-auto" /> : 'Save & Connect'}
        </button>
        <button
          onClick={() => { setShowForm(false); setPat(''); }}
          className="px-3 py-2 rounded-md border border-gray-300 dark:border-gray-600 text-sm text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 transition"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}
