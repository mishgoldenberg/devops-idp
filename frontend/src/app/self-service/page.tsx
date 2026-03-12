'use client';

import { useState, useEffect, useCallback } from 'react';
import { Card } from '@/components/common/Card';
import { Badge } from '@/components/common/Badge';
import {
  Plus,
  X,
  Package,
  CheckCircle,
  ArrowRight,
  AlertCircle,
  ExternalLink,
} from 'lucide-react';
import { CreateProjectForm, ProjectCreationResult } from '@/components/self-service/CreateProjectForm';
import { apiClient } from '@/lib/api-client';

// ── Types ─────────────────────────────────────────────────────────────────────

type ProvisioningState =
  | { type: 'idle' }
  | {
      type: 'provisioning';
      job_id: string;
      project_name: string;
      process_type: string;
      /** Unix ms when provisioning started — used for the 8-min timeout. */
      startedAt: number;
    }
  | {
      type: 'success';
      project_name: string;
      process_type: string;
      project_url: string;
    }
  | {
      type: 'error';
      project_name: string;
      message: string;
    };

// ── Loading steps shown during provisioning ───────────────────────────────────

const LOADING_STEPS = [
  'Initialising Terraform workspace…',
  'Downloading provider microsoft/azuredevops…',
  'Creating custom inherited process…',
  'Provisioning Azure DevOps project…',
  'Applying final configuration…',
];

// ── Static data ───────────────────────────────────────────────────────────────

const stats = [
  {
    label: 'Projects Created',
    value: '3',
    trend: '+2 this month',
    icon: Package,
    color: 'text-blue-600 dark:text-blue-400',
  },
];

const statusColors: Record<string, string> = {
  Available: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400',
};

// ── Page ──────────────────────────────────────────────────────────────────────

export default function SelfServicePage() {
  const [showModal, setShowModal] = useState(false);
  const [state, setState] = useState<ProvisioningState>({ type: 'idle' });
  const [loadingStep, setLoadingStep] = useState(0);

  // ── Cycle through loading step labels while provisioning ──────────────────
  useEffect(() => {
    if (state.type !== 'provisioning') return;
    const id = setInterval(() => {
      setLoadingStep((s) => (s + 1) % LOADING_STEPS.length);
    }, 3500);
    return () => clearInterval(id);
  }, [state.type]);

  // ── Poll Terraform job status ─────────────────────────────────────────────
  // Extract primitives so the callback/effect only re-create when these values
  // actually change (avoids object-identity re-runs on unrelated renders).
  const jobId = state.type === 'provisioning' ? state.job_id : null;
  const jobProjectName = state.type === 'provisioning' ? state.project_name : '';
  const jobProcessType = state.type === 'provisioning' ? state.process_type : '';
  const jobStartedAt = state.type === 'provisioning' ? state.startedAt : 0;

  const POLL_TIMEOUT_MS = 8 * 60 * 1000; // 8 minutes

  const poll = useCallback(async () => {
    if (!jobId) return;

    // Hard timeout — surface a clear error instead of spinning forever.
    if (Date.now() - jobStartedAt > POLL_TIMEOUT_MS) {
      setState({
        type: 'error',
        project_name: jobProjectName,
        message:
          'Provisioning timed out after 8 minutes. ' +
          'Check the Terraform job logs: ' +
          `kubectl logs -n devops-control-center -l app=terraform-runner --tail=100`,
      });
      return;
    }

    try {
      const res = await apiClient.getProjectCreationStatus(jobId);
      const { status: jobStatus, project_url, error } = res.data;

      if (jobStatus === 'succeeded' && project_url) {
        setState({
          type: 'success',
          project_name: jobProjectName,
          process_type: jobProcessType,
          project_url,
        });
      } else if (jobStatus === 'failed' || jobStatus === 'error') {
        setState({
          type: 'error',
          project_name: jobProjectName,
          message: error ?? 'Project creation failed.',
        });
      }
    } catch {
      // Transient network error — keep polling
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId, jobProjectName, jobProcessType, jobStartedAt]);

  useEffect(() => {
    if (!jobId) return;
    poll(); // immediate first poll
    const id = setInterval(poll, 3000);
    return () => clearInterval(id);
  }, [jobId, poll]);

  // ── Form success handler ──────────────────────────────────────────────────
  const handleFormSuccess = (result: ProjectCreationResult) => {
    setShowModal(false);
    setLoadingStep(0);
    setState({
      type: 'provisioning',
      job_id: result.job_id,
      project_name: result.project_name,
      process_type: result.process_type,
      startedAt: Date.now(),
    });
  };

  const services = [
    {
      id: 1,
      title: 'Create Project',
      description:
        'Instantly provision a new Azure DevOps project with your choice of process template (Scrum, Agile, CMMI, or Basic).',
      icon: Plus,
      iconBg: 'bg-blue-600',
      badge: 'Available',
      action: 'Create',
    },
  ];

  return (
    <div className="min-h-screen bg-gray-50 dark:bg-gray-900/50">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-12 space-y-10">

        {/* Page Header */}
        <div className="space-y-3">
          <h1 className="text-4xl font-bold tracking-tight text-gray-900 dark:text-white">
            Self-Service
          </h1>
          <p className="text-lg text-gray-600 dark:text-gray-400">
            Create and manage Azure DevOps projects on demand
          </p>
        </div>

        {/* Stats */}
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
          {stats.map((stat) => {
            const Icon = stat.icon;
            return (
              <Card
                key={stat.label}
                className="p-6 bg-white dark:bg-gray-800 border-0 shadow-sm hover:shadow-md transition-shadow"
              >
                <div className="flex items-start justify-between">
                  <div>
                    <p className="text-sm font-medium text-gray-600 dark:text-gray-400">
                      {stat.label}
                    </p>
                    <p className="text-3xl font-bold text-gray-900 dark:text-white mt-3">
                      {stat.value}
                    </p>
                    <p className="text-sm text-gray-500 dark:text-gray-400 mt-2">
                      {stat.trend}
                    </p>
                  </div>
                  <Icon className={`w-8 h-8 ${stat.color}`} />
                </div>
              </Card>
            );
          })}
        </div>

        {/* ── Provisioning / Success / Error banner ── */}

        {state.type === 'provisioning' && (
          <Card className="p-8 bg-white dark:bg-gray-800 border border-blue-200 dark:border-blue-800/50 shadow-sm">
            <div className="flex items-start gap-5">
              {/* Spinner */}
              <div className="shrink-0 mt-1">
                <div className="w-10 h-10 rounded-full border-4 border-blue-100 dark:border-blue-900 border-t-blue-600 animate-spin" />
              </div>
              <div className="flex-1 space-y-3">
                <h3 className="text-lg font-semibold text-gray-900 dark:text-white">
                  Provisioning <span className="text-blue-600 dark:text-blue-400 font-mono">{state.project_name}</span>…
                </h3>
                {/* Step progress */}
                <div className="space-y-1.5">
                  {LOADING_STEPS.map((step, i) => (
                    <div key={step} className="flex items-center gap-2">
                      <span
                        className={
                          'w-1.5 h-1.5 rounded-full shrink-0 transition-colors duration-500 ' +
                          (i < loadingStep
                            ? 'bg-green-500'
                            : i === loadingStep
                            ? 'bg-blue-500 animate-pulse'
                            : 'bg-gray-200 dark:bg-gray-700')
                        }
                      />
                      <span
                        className={
                          'text-sm transition-colors duration-300 ' +
                          (i < loadingStep
                            ? 'text-green-600 dark:text-green-400 line-through'
                            : i === loadingStep
                            ? 'text-blue-700 dark:text-blue-300 font-medium'
                            : 'text-gray-400 dark:text-gray-600')
                        }
                      >
                        {step}
                      </span>
                    </div>
                  ))}
                </div>
                <p className="text-xs text-gray-400 dark:text-gray-500 pt-1">
                  Terraform is running in a container — this typically takes 30–90 seconds.
                </p>
              </div>
            </div>
          </Card>
        )}

        {state.type === 'success' && (
          <Card className="p-8 bg-gradient-to-br from-green-50 to-green-50/50 dark:from-green-900/20 dark:to-green-900/10 border-2 border-green-200 dark:border-green-800/50 shadow-sm">
            <div className="flex items-start gap-4">
              <CheckCircle className="w-6 h-6 text-green-600 dark:text-green-400 shrink-0 mt-0.5" />
              <div className="flex-1">
                <h3 className="text-xl font-semibold text-green-900 dark:text-green-100 mb-4">
                  Project Created Successfully!
                </h3>
                <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4 mb-6">
                  <InfoTile label="Project Name" value={state.project_name} mono />
                  <InfoTile label="Process Type" value={`${state.project_name}-${state.process_type}`} mono />
                </div>
                <div className="pt-4 border-t border-green-300/50 dark:border-green-700/30">
                  <a
                    href={state.project_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-2 px-5 py-2.5 bg-green-600 hover:bg-green-700 text-white font-semibold rounded-lg transition-all duration-200 hover:shadow-md"
                  >
                    Go to Project
                    <ExternalLink className="w-4 h-4" />
                  </a>
                </div>
              </div>
            </div>
          </Card>
        )}

        {state.type === 'error' && (
          <Card className="p-6 bg-red-50 dark:bg-red-900/20 border-2 border-red-200 dark:border-red-800/50 shadow-sm">
            <div className="flex items-start gap-3">
              <AlertCircle className="w-5 h-5 text-red-600 dark:text-red-400 shrink-0 mt-0.5" />
              <div>
                <p className="font-semibold text-red-900 dark:text-red-100">
                  Could not create project
                </p>
                <p className="text-sm text-red-700 dark:text-red-300 mt-1">
                  {state.message}
                </p>
                <button
                  onClick={() => setState({ type: 'idle' })}
                  className="mt-3 text-sm text-red-600 dark:text-red-400 underline hover:no-underline"
                >
                  Try again
                </button>
              </div>
            </div>
          </Card>
        )}

        {/* ── Available Services ── */}
        <div>
          <h2 className="text-2xl font-bold text-gray-900 dark:text-white mb-6">
            Available Services
          </h2>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
            {services.map((service) => {
              const Icon = service.icon;
              const isProvisioning = state.type === 'provisioning';
              return (
                <Card
                  key={service.id}
                  className={
                    'p-6 bg-white dark:bg-gray-800 border-0 shadow-sm hover:shadow-xl ' +
                    'transition-all duration-300 relative overflow-hidden group ' +
                    (isProvisioning ? 'opacity-60 cursor-not-allowed' : 'cursor-pointer')
                  }
                  onClick={() => !isProvisioning && setShowModal(true)}
                >
                  <div className="absolute inset-0 bg-gradient-to-br from-blue-500/0 to-blue-500/0 group-hover:from-blue-500/5 group-hover:to-blue-500/10 transition-all duration-300 pointer-events-none" />
                  <div className="relative space-y-4">
                    <div className="flex items-start justify-between">
                      <div
                        className={`${service.iconBg} p-3 rounded-lg group-hover:scale-110 transition-transform duration-300`}
                      >
                        <Icon className="w-6 h-6 text-white" />
                      </div>
                      <Badge className={statusColors[service.badge]}>
                        {service.badge}
                      </Badge>
                    </div>
                    <div>
                      <h3 className="text-lg font-semibold text-gray-900 dark:text-white group-hover:text-blue-600 dark:group-hover:text-blue-400 transition-colors">
                        {service.title}
                      </h3>
                      <p className="text-gray-600 dark:text-gray-400 text-sm mt-2 leading-relaxed">
                        {service.description}
                      </p>
                    </div>
                    <div className="pt-2">
                      <span className="inline-flex items-center gap-2 text-blue-600 dark:text-blue-400 hover:text-blue-700 dark:hover:text-blue-300 font-semibold text-sm group-hover:gap-3 transition-all duration-300">
                        {service.action}
                        <Plus className="w-4 h-4 group-hover:rotate-90 transition-transform duration-300" />
                      </span>
                    </div>
                  </div>
                </Card>
              );
            })}
          </div>
        </div>
      </div>

      {/* ── Create Project Modal ── */}
      {showModal && (
        <div className="fixed inset-0 bg-black/50 dark:bg-black/70 z-50 flex items-center justify-center p-4 backdrop-blur-sm animate-in fade-in duration-300">
          <Card className="w-full max-w-lg p-0 max-h-[92vh] overflow-hidden shadow-2xl animate-in zoom-in-95 duration-300 bg-white dark:bg-gray-800">

            {/* Modal header */}
            <div className="bg-gradient-to-r from-blue-600 to-blue-700 px-8 py-6 flex items-center justify-between">
              <h2 className="text-2xl font-bold text-white">Create New Project</h2>
              <button
                onClick={() => setShowModal(false)}
                className="p-2 hover:bg-blue-500/20 rounded-lg transition-all duration-200 text-white"
                title="Close"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            {/* Modal body */}
            <div className="p-8 overflow-y-auto max-h-[calc(92vh-88px)]">
              <CreateProjectForm
                onSuccess={handleFormSuccess}
                onClose={() => setShowModal(false)}
              />
            </div>
          </Card>
        </div>
      )}
    </div>
  );
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function InfoTile({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="bg-white/60 dark:bg-gray-800/40 rounded-lg p-4 border border-green-200/50 dark:border-green-800/30">
      <span className="text-xs font-medium text-green-900 dark:text-green-300 uppercase tracking-wider">
        {label}
      </span>
      <p className={`text-base font-semibold text-green-700 dark:text-green-300 mt-2 ${mono ? 'font-mono' : ''}`}>
        {value}
      </p>
    </div>
  );
}
