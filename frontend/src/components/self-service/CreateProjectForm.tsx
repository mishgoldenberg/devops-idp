'use client';

import { useState } from 'react';
import { Button } from '@/components/common/Button';
import { apiClient } from '@/lib/api-client';
import { HelpCircle, ChevronRight, ArrowLeft, AlertCircle } from 'lucide-react';

// ── Types ─────────────────────────────────────────────────────────────────────

export interface ProjectCreationResult {
  job_id: string;
  project_name: string;
  process_type: string;
  admin_username: string;
}

interface CreateProjectFormProps {
  onSuccess: (result: ProjectCreationResult) => void;
  onClose: () => void;
}

// ── Constants ─────────────────────────────────────────────────────────────────

const PROCESS_TYPES = ['Scrum', 'Agile', 'CMMI', 'Basic'] as const;

const PROCESS_DESCRIPTIONS: Record<string, string> = {
  Scrum: 'Sprint-based iterative delivery with backlogs, velocity, and burndown charts.',
  Agile: 'Flexible work-tracking with user stories, tasks, issues, and epics.',
  CMMI: 'Formal process for regulated or compliance-driven environments.',
  Basic: 'Minimal setup — epics, issues, and tasks only.',
};

const HINTS = {
  project_name:
    'Lowercase letters, digits, hyphens and spaces only. ' +
    'Max 64 characters. Example: my-platform or data-pipeline-2025.',
  process_type:
    'A custom inherited process named "<project>-<process>" will be created in ' +
    "Azure DevOps and used as this project's work-item template.",
  admin_username:
    "The Azure DevOps principal name (usually the user's e-mail address). " +
    'The account must already exist in your Azure DevOps organisation.',
};

// ── Tooltip ───────────────────────────────────────────────────────────────────

function Tooltip({ text }: { text: string }) {
  return (
    <span className="relative group inline-flex items-center ml-1.5">
      <HelpCircle className="w-3.5 h-3.5 text-gray-400 dark:text-gray-500 cursor-help" />
      <span
        className={[
          'pointer-events-none absolute left-1/2 -translate-x-1/2 bottom-full mb-2 z-50',
          'w-64 rounded-lg bg-gray-900 dark:bg-gray-700 text-white text-xs px-3 py-2 shadow-xl',
          'opacity-0 group-hover:opacity-100 transition-opacity duration-200 leading-relaxed',
        ].join(' ')}
      >
        {text}
        <span className="absolute top-full left-1/2 -translate-x-1/2 border-4 border-transparent border-t-gray-900 dark:border-t-gray-700" />
      </span>
    </span>
  );
}

// ── Error banner ──────────────────────────────────────────────────────────────

function ErrorBanner({ message }: { message: string }) {
  return (
    <div className="flex items-start gap-3 p-4 bg-red-50 dark:bg-red-900/20 border-l-4 border-red-500 rounded-lg animate-in fade-in">
      <AlertCircle className="w-4 h-4 text-red-600 dark:text-red-400 shrink-0 mt-0.5" />
      <div>
        <p className="font-semibold text-red-900 dark:text-red-100 text-sm">Error</p>
        <p className="text-red-700 dark:text-red-300 text-sm mt-0.5">{message}</p>
      </div>
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

export function CreateProjectForm({ onSuccess, onClose }: CreateProjectFormProps) {
  const [step, setStep] = useState<'form' | 'confirming' | 'submitting'>('form');

  const [projectName, setProjectName] = useState('');
  const [processType, setProcessType] = useState<string>('Scrum');
  const [adminUsername, setAdminUsername] = useState('golden.mihel@gmail.com');

  // Field-level errors (only for 409 / 422)
  const [fieldError, setFieldError] = useState<{
    field: 'project_name' | 'admin_username' | null;
    message: string;
  }>({ field: null, message: '' });

  // General error banner (for 500 / 503 / network errors)
  const [generalError, setGeneralError] = useState('');

  const clearErrors = () => {
    setFieldError({ field: null, message: '' });
    setGeneralError('');
  };

  // ── Step: form ──────────────────────────────────────────────────────────

  const handleFormSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    clearErrors();
    setStep('confirming');
  };

  // ── Step: confirm → submit ──────────────────────────────────────────────

  const handleConfirm = async () => {
    clearErrors();
    setStep('submitting');

    try {
      const result = await apiClient.createProject({
        project_name: projectName.trim(),
        process_type: processType,
        admin_username: adminUsername.trim(),
      });

      onSuccess({
        job_id: result.data.job_id,
        project_name: projectName.trim(),
        process_type: processType,
        admin_username: adminUsername.trim(),
      });
    } catch (err: any) {
      const httpStatus: number = err?.response?.status ?? 0;
      const detail: string =
        err?.response?.data?.detail ?? err?.message ?? 'An unexpected error occurred.';

      if (httpStatus === 409) {
        setFieldError({ field: 'project_name', message: detail });
        setStep('form');
      } else if (httpStatus === 422) {
        setFieldError({ field: 'admin_username', message: detail });
        setStep('form');
      } else {
        // 500 / 503 / network error — show a visible banner and stay on confirm
        // so the user can see their inputs and retry without re-typing everything.
        setGeneralError(detail);
        setStep('confirming');
      }
    }
  };

  // ── Render: submitting ──────────────────────────────────────────────────

  if (step === 'submitting') {
    return (
      <div className="flex flex-col items-center justify-center py-10 gap-4">
        <div className="w-12 h-12 rounded-full border-4 border-blue-200 dark:border-blue-900 border-t-blue-600 animate-spin" />
        <p className="text-gray-700 dark:text-gray-300 font-medium">
          Submitting to Terraform&hellip;
        </p>
        <p className="text-sm text-gray-500 dark:text-gray-400 text-center max-w-xs">
          Preparing infrastructure job. You will see live progress shortly.
        </p>
      </div>
    );
  }

  // ── Render: confirming ──────────────────────────────────────────────────

  if (step === 'confirming') {
    return (
      <div className="space-y-5">
        {generalError && <ErrorBanner message={generalError} />}

        <div className="space-y-1">
          <h3 className="text-lg font-semibold text-gray-900 dark:text-white">
            Confirm project details
          </h3>
          <p className="text-sm text-gray-500 dark:text-gray-400">
            Review the settings below before Terraform provisions your project.
          </p>
        </div>

        <div className="rounded-xl border border-gray-200 dark:border-gray-700 overflow-hidden divide-y divide-gray-100 dark:divide-gray-700">
          <ConfirmRow label="Project name" value={projectName.trim()} mono />
          <ConfirmRow
            label="Process template"
            value={`${projectName.trim()}-${processType}`}
            mono
            sub={PROCESS_DESCRIPTIONS[processType]}
          />
          <ConfirmRow label="Admin user" value={adminUsername.trim()} mono />
        </div>

        <p className="text-xs text-gray-500 dark:text-gray-400 bg-gray-50 dark:bg-gray-800/60 rounded-lg px-4 py-3 border border-gray-200 dark:border-gray-700">
          Terraform will run in a container under the{' '}
          <span className="font-mono">devops-terraform-sa</span> service account. State is
          stored in{' '}
          <span className="font-mono">gs://devops-control-center-tfstate</span>.
        </p>

        <div className="flex gap-3 pt-1">
          <Button
            type="button"
            onClick={handleConfirm}
            className="flex-1 py-2.5 bg-blue-600 hover:bg-blue-700 text-white font-semibold rounded-lg transition-all"
          >
            <span className="flex items-center justify-center gap-2">
              Confirm &amp; Create
              <ChevronRight className="w-4 h-4" />
            </span>
          </Button>
          <button
            type="button"
            onClick={() => { clearErrors(); setStep('form'); }}
            className="flex-1 px-4 py-2.5 border-2 border-gray-300 dark:border-gray-600 rounded-lg text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors font-semibold flex items-center justify-center gap-2"
          >
            <ArrowLeft className="w-4 h-4" />
            Back
          </button>
        </div>
      </div>
    );
  }

  // ── Render: form (default) ──────────────────────────────────────────────

  return (
    <form onSubmit={handleFormSubmit} className="space-y-6 w-full">

      {/* General error banner (shown here after a field-level error on back-navigation) */}
      {generalError && <ErrorBanner message={generalError} />}

      {/* Project Name */}
      <div className="space-y-2">
        <label
          htmlFor="project-name"
          className="flex items-center text-sm font-semibold text-gray-900 dark:text-gray-100"
        >
          Project Name <span className="text-red-500 ml-0.5">*</span>
          <Tooltip text={HINTS.project_name} />
        </label>
        <input
          id="project-name"
          type="text"
          value={projectName}
          onChange={(e) => { setProjectName(e.target.value); clearErrors(); }}
          placeholder="e.g. my-platform"
          required
          className={[
            'w-full px-4 py-2.5 text-base border-2 rounded-lg',
            'bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100',
            'placeholder-gray-400 dark:placeholder-gray-500',
            'focus:outline-none focus:ring-2 transition-all',
            fieldError.field === 'project_name'
              ? 'border-red-500 focus:border-red-500 focus:ring-red-200 dark:focus:ring-red-900/50'
              : 'border-gray-200 dark:border-gray-600 focus:border-blue-500 focus:ring-blue-200 dark:focus:ring-blue-900/50',
          ].join(' ')}
        />
        {fieldError.field === 'project_name' ? (
          <p className="text-xs text-red-600 dark:text-red-400 font-medium">
            {fieldError.message}
          </p>
        ) : (
          <p className="text-xs text-gray-500 dark:text-gray-400">
            Lowercase letters, digits, hyphens only. Example: data-pipeline-2025
          </p>
        )}
      </div>

      {/* Process Type */}
      <div className="space-y-2">
        <label
          htmlFor="process-type"
          className="flex items-center text-sm font-semibold text-gray-900 dark:text-gray-100"
        >
          Process Type <span className="text-red-500 ml-0.5">*</span>
          <Tooltip text={HINTS.process_type} />
        </label>
        <select
          id="process-type"
          value={processType}
          onChange={(e) => setProcessType(e.target.value)}
          className="w-full px-4 py-2.5 text-base border-2 border-gray-200 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 focus:outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-200 dark:focus:ring-blue-900/50 transition-all"
        >
          {PROCESS_TYPES.map((t) => (
            <option key={t} value={t}>{t}</option>
          ))}
        </select>
        <p className="text-xs text-gray-500 dark:text-gray-400">
          {PROCESS_DESCRIPTIONS[processType]}
        </p>
      </div>

      {/* Admin User */}
      <div className="space-y-2">
        <label
          htmlFor="admin-username"
          className="flex items-center text-sm font-semibold text-gray-900 dark:text-gray-100"
        >
          Admin User <span className="text-red-500 ml-0.5">*</span>
          <Tooltip text={HINTS.admin_username} />
        </label>
        <input
          id="admin-username"
          type="email"
          value={adminUsername}
          onChange={(e) => { setAdminUsername(e.target.value); clearErrors(); }}
          placeholder="user@company.com"
          required
          className={[
            'w-full px-4 py-2.5 text-base border-2 rounded-lg',
            'bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100',
            'placeholder-gray-400 dark:placeholder-gray-500',
            'focus:outline-none focus:ring-2 transition-all',
            fieldError.field === 'admin_username'
              ? 'border-red-500 focus:border-red-500 focus:ring-red-200 dark:focus:ring-red-900/50'
              : 'border-gray-200 dark:border-gray-600 focus:border-blue-500 focus:ring-blue-200 dark:focus:ring-blue-900/50',
          ].join(' ')}
        />
        {fieldError.field === 'admin_username' ? (
          <p className="text-xs text-red-600 dark:text-red-400 font-medium">
            {fieldError.message}
          </p>
        ) : (
          <p className="text-xs text-gray-500 dark:text-gray-400">
            Must be an existing Azure DevOps user in your organisation.
          </p>
        )}
      </div>

      {/* Actions */}
      <div className="flex gap-3 pt-2">
        <Button
          type="submit"
          disabled={!projectName.trim() || !adminUsername.trim()}
          className="flex-1 py-2.5 bg-blue-600 hover:bg-blue-700 text-white font-semibold rounded-lg transition-all disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2"
        >
          Review &amp; Confirm
          <ChevronRight className="w-4 h-4" />
        </Button>
        <button
          type="button"
          onClick={onClose}
          className="flex-1 px-4 py-2.5 border-2 border-gray-300 dark:border-gray-600 rounded-lg text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors font-semibold"
        >
          Cancel
        </button>
      </div>
    </form>
  );
}

// ── Confirm row ───────────────────────────────────────────────────────────────

function ConfirmRow({
  label,
  value,
  sub,
  mono = false,
  shade = false,
}: {
  label: string;
  value: string;
  sub?: string;
  mono?: boolean;
  shade?: boolean;
}) {
  return (
    <div
      className={[
        'flex items-start gap-4 px-5 py-4',
        shade
          ? 'bg-gray-50 dark:bg-gray-800'
          : 'bg-white dark:bg-gray-800/50',
      ].join(' ')}
    >
      <span className="w-36 shrink-0 text-xs font-semibold uppercase tracking-wider text-gray-500 dark:text-gray-400 pt-0.5">
        {label}
      </span>
      <span className="flex-1 min-w-0">
        <span
          className={[
            'text-sm font-semibold text-gray-900 dark:text-white break-all block',
            mono ? 'font-mono' : '',
          ].join(' ')}
        >
          {value}
        </span>
        {sub && (
          <span className="text-xs text-gray-500 dark:text-gray-400 mt-0.5 block">
            {sub}
          </span>
        )}
      </span>
    </div>
  );
}
