'use client';

import { useState } from 'react';
import { Button } from '@/components/common/Button';
import { apiClient } from '@/lib/api-client';

const PROCESS_TYPES = ['Scrum', 'Agile', 'CMMI', 'Basic'];

interface CreateProjectFormProps {
  onSuccess: (project: any) => void;
  onClose: () => void;
}

export function CreateProjectForm({ onSuccess, onClose }: CreateProjectFormProps) {
  const [projectName, setProjectName] = useState('');
  const [processType, setProcessType] = useState('Scrum');
  const [adminUsername, setAdminUsername] = useState('golden.mihel@gmail.com');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setLoading(true);

    try {
      if (!projectName.trim()) {
        throw new Error('Project name is required');
      }
      if (!adminUsername.trim()) {
        throw new Error('Admin username is required');
      }

      // Get token - will be available at submission time
      const token = apiClient.getToken();
      if (!token) {
        throw new Error('Not authenticated. Please log in first.');
      }

      const payload = {
        project_name: projectName.trim(),
        process_type: processType,
        admin_username: adminUsername.trim(),
      };

      const response = await fetch('http://localhost:8000/api/azure-devops/projects/create', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify(payload),
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.detail || `Failed to create project (${response.status})`);
      }

      const data = await response.json();
      onSuccess({
        project_name: projectName,
        process_type: processType,
        admin_username: adminUsername,
        project_url: data.data.project_url,
        added_admin: data.data.added_admin,
      });

      // Reset form
      setProjectName('');
      setProcessType('Scrum');
      setAdminUsername('golden.mihel@gmail.com');
    } catch (err: any) {
      setError(err.message || 'An error occurred');
      console.error('Project creation error:', err);
    } finally {
      setLoading(false);
    }
  };

  return (
    <form onSubmit={handleSubmit} className="space-y-6 w-full">
      {error && (
        <div className="p-4 bg-red-50 dark:bg-red-900/20 border-l-4 border-red-500 rounded-lg text-red-700 dark:text-red-200 text-sm animate-in fade-in">
          <p className="font-semibold text-red-900 dark:text-red-100 mb-1">Error</p>
          <p className="text-red-700 dark:text-red-300">{error}</p>
        </div>
      )}

      <div className="space-y-2">
        <label htmlFor="project-name" className="block text-sm font-semibold text-gray-900 dark:text-gray-100">
          Project Name <span className="text-red-500">*</span>
        </label>
        <input
          id="project-name"
          type="text"
          value={projectName}
          onChange={(e) => setProjectName(e.target.value)}
          placeholder="e.g., my-platform"
          disabled={loading}
          className="w-full px-4 py-2.5 text-base border-2 border-gray-200 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500 focus:outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-200 dark:focus:ring-blue-900/50 disabled:opacity-50 disabled:bg-gray-100 disabled:cursor-not-allowed transition-all"
        />
        <p className="text-xs text-gray-500 dark:text-gray-400">
          Lowercase letters, numbers, and hyphens only
        </p>
      </div>

      <div className="space-y-2">
        <label htmlFor="process-type" className="block text-sm font-semibold text-gray-900 dark:text-gray-100">
          Process Type <span className="text-red-500">*</span>
        </label>
        <select
          id="process-type"
          value={processType}
          onChange={(e) => setProcessType(e.target.value)}
          disabled={loading}
          className="w-full px-4 py-2.5 text-base border-2 border-gray-200 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 focus:outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-200 dark:focus:ring-blue-900/50 disabled:opacity-50 disabled:bg-gray-100 disabled:cursor-not-allowed transition-all"
        >
          {PROCESS_TYPES.map((type) => (
            <option key={type} value={type}>
              {type}
            </option>
          ))}
        </select>
        <p className="text-xs text-gray-500 dark:text-gray-400">
          Choose Scrum for iterations, Agile for flexible work, CMMI for heavy process, or Basic for simple tracking
        </p>
      </div>

      <div className="space-y-2">
        <label htmlFor="admin-username" className="block text-sm font-semibold text-gray-900 dark:text-gray-100">
          Admin User Email <span className="text-red-500">*</span>
        </label>
        <input
          id="admin-username"
          type="email"
          value={adminUsername}
          onChange={(e) => setAdminUsername(e.target.value)}
          placeholder="user@company.com"
          disabled={loading}
          className="w-full px-4 py-2.5 text-base border-2 border-gray-200 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500 focus:outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-200 dark:focus:ring-blue-900/50 disabled:opacity-50 disabled:bg-gray-100 disabled:cursor-not-allowed transition-all"
        />
        <p className="text-xs text-gray-500 dark:text-gray-400">
          Must be an existing Azure AD user
        </p>
      </div>

      <div className="flex gap-3 pt-2">
        <Button
          type="submit"
          disabled={loading || !projectName.trim()}
          className="flex-1 py-2.5 bg-blue-600 hover:bg-blue-700 text-white font-semibold rounded-lg transition-all disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {loading ? (
            <span className="flex items-center justify-center gap-2">
              <span className="inline-block animate-spin">⚙️</span>
              Creating...
            </span>
          ) : (
            'Create Project'
          )}
        </Button>
        <button
          type="button"
          onClick={onClose}
          disabled={loading}
          className="flex-1 px-4 py-2.5 border-2 border-gray-300 dark:border-gray-600 rounded-lg text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors font-semibold disabled:opacity-50 disabled:cursor-not-allowed"
        >
          Cancel
        </button>
      </div>
    </form>
  );
}
