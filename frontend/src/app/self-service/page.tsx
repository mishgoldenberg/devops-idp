'use client';

import { useState, useEffect } from 'react';
import { Card } from '@/components/common/Card';
import { Badge } from '@/components/common/Badge';
import { Plus, X, Package, CheckCircle, ArrowRight, Loader } from 'lucide-react';
import { CreateProjectForm } from '@/components/self-service/CreateProjectForm';

const stats = [
  { label: 'Projects Created', value: '3', trend: '+2 this month', icon: Package, color: 'text-blue-600 dark:text-blue-400' },
];

export default function SelfServicePage() {
  const [showModal, setShowModal] = useState(false);
  const [createdProject, setCreatedProject] = useState<any>(null);
  const [isButtonReady, setIsButtonReady] = useState(false);

  // 3-second delay before button becomes clickable
  useEffect(() => {
    if (createdProject) {
      setIsButtonReady(false);
      const timer = setTimeout(() => {
        setIsButtonReady(true);
      }, 3000);
      return () => clearTimeout(timer);
    }
  }, [createdProject]);

  const services = [
    {
      id: 1,
      title: 'Create Project',
      description: 'Instantly provision a new Azure DevOps project with your choice of process template (Scrum, Agile, CMMI, or Basic).',
      icon: Plus,
      iconBg: 'bg-blue-600',
      badge: 'Available',
      action: 'Create',
    },
  ];

  const statusColors = {
    Available: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400',
  };

  return (
    <div className="min-h-screen bg-gray-50 dark:bg-gray-900/50">
      {/* Main Content */}
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

        {/* Stats Section */}
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
          {stats.map((stat) => {
            const Icon = stat.icon;
            return (
              <Card key={stat.label} className="p-6 bg-white dark:bg-gray-800 border-0 shadow-sm hover:shadow-md transition-shadow">
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

        {/* Success Message */}
        {createdProject && (
          <Card className="p-8 bg-gradient-to-br from-green-50 to-green-50/50 dark:from-green-900/20 dark:to-green-900/10 border-2 border-green-200 dark:border-green-800/50 shadow-sm">
            <div className="flex items-start gap-4">
              <CheckCircle className="w-6 h-6 text-green-600 dark:text-green-400 flex-shrink-0 mt-0.5" />
              <div className="flex-1">
                <h3 className="text-xl font-semibold text-green-900 dark:text-green-100 mb-4">
                  🎉 Project Created Successfully!
                </h3>
                <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4 mb-6">
                  <div className="bg-white/60 dark:bg-gray-800/40 rounded-lg p-4 border border-green-200/50 dark:border-green-800/30">
                    <span className="text-xs font-medium text-green-900 dark:text-green-300 uppercase tracking-wider">Project Name</span>
                    <p className="text-base font-mono text-green-700 dark:text-green-300 mt-2 font-semibold">
                      {createdProject.project_name}
                    </p>
                  </div>
                  <div className="bg-white/60 dark:bg-gray-800/40 rounded-lg p-4 border border-green-200/50 dark:border-green-800/30">
                    <span className="text-xs font-medium text-green-900 dark:text-green-300 uppercase tracking-wider">Process Type</span>
                    <p className="text-base font-semibold text-green-700 dark:text-green-300 mt-2">
                      {createdProject.process_type}
                    </p>
                  </div>
                </div>
                {createdProject.project_url && (
                  <div className="pt-4 border-t border-green-300/50 dark:border-green-700/30">
                    {isButtonReady ? (
                      <a
                        href={createdProject.project_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-2 px-4 py-2 bg-green-600 hover:bg-green-700 text-white font-medium rounded-lg transition-all duration-200 hover:shadow-md"
                      >
                        Open in Azure DevOps
                        <ArrowRight className="w-4 h-4" />
                      </a>
                    ) : (
                      <div className="flex items-center gap-2 text-gray-700 dark:text-gray-300">
                        <Loader className="w-5 h-5 animate-spin" />
                        <span>Creating project in Azure DevOps…</span>
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>
          </Card>
        )}

        {/* Available Services Section */}
        <div>
          <h2 className="text-2xl font-bold text-gray-900 dark:text-white mb-6">
            Available Services
          </h2>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
            {services.map((service) => {
              const Icon = service.icon;
              return (
                <Card
                  key={service.id}
                  className="p-6 bg-white dark:bg-gray-800 border-0 shadow-sm hover:shadow-xl transition-all duration-300 cursor-pointer group relative overflow-hidden"
                  onClick={() => setShowModal(true)}
                >
                  {/* Animated gradient overlay */}
                  <div className="absolute inset-0 bg-gradient-to-br from-blue-500/0 to-blue-500/0 group-hover:from-blue-500/5 group-hover:to-blue-500/10 transition-all duration-300 pointer-events-none" />
                  
                  <div className="relative space-y-4">
                    {/* Header with icon and badge */}
                    <div className="flex items-start justify-between">
                      <div className={`${service.iconBg} p-3 rounded-lg group-hover:scale-110 transition-transform duration-300`}>
                        <Icon className="w-6 h-6 text-white" />
                      </div>
                      <Badge className={statusColors[service.badge as keyof typeof statusColors]}>
                        {service.badge}
                      </Badge>
                    </div>

                    {/* Content */}
                    <div>
                      <h3 className="text-lg font-semibold text-gray-900 dark:text-white group-hover:text-blue-600 dark:group-hover:text-blue-400 transition-colors">
                        {service.title}
                      </h3>
                      <p className="text-gray-600 dark:text-gray-400 text-sm mt-2 leading-relaxed">
                        {service.description}
                      </p>
                    </div>

                    {/* Action button */}
                    <div className="pt-2">
                      <button className="inline-flex items-center gap-2 text-blue-600 dark:text-blue-400 hover:text-blue-700 dark:hover:text-blue-300 font-semibold text-sm group-hover:gap-3 transition-all duration-300">
                        {service.action}
                        <Plus className="w-4 h-4 group-hover:rotate-90 transition-transform duration-300" />
                      </button>
                    </div>
                  </div>
                </Card>
              );
            })}
          </div>
        </div>
      </div>

      {/* Modal Overlay */}
      {showModal && (
        <div className="fixed inset-0 bg-black/50 dark:bg-black/70 z-50 flex items-center justify-center p-4 backdrop-blur-sm animate-in fade-in duration-300">
          <Card className="w-full max-w-md p-0 max-h-[90vh] overflow-hidden shadow-2xl animate-in zoom-in-95 duration-300 bg-white dark:bg-gray-800">
            
            {/* Modal Header with Gradient */}
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

            {/* Modal Body */}
            <div className="p-8 overflow-y-auto max-h-[calc(90vh-88px)]">
              <CreateProjectForm
                onSuccess={(project) => {
                  setCreatedProject(project);
                  setShowModal(false);
                }}
                onClose={() => setShowModal(false)}
              />
            </div>
          </Card>
        </div>
      )}
    </div>
  );
}
