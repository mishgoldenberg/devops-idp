'use client';

import { Card } from '@/components/common/Card';
import { Badge } from '@/components/common/Badge';
import { 
  Server, GitBranch, Shield, Database, HelpCircle, Settings,
  Check, Clock, XCircle, ArrowRight
} from 'lucide-react';

const requestTypes = [
  {
    id: 'infrastructure',
    title: 'Infrastructure Request',
    description: 'Request new servers, databases, or compute resources',
    icon: Server,
    color: 'from-blue-500 to-blue-600',
    bgColor: 'bg-blue-50',
    borderColor: 'border-blue-200',
    iconBg: 'bg-blue-600',
  },
  {
    id: 'pipeline',
    title: 'Pipeline Request',
    description: 'Setup CI/CD pipelines and deployment workflows',
    icon: GitBranch,
    color: 'from-green-500 to-green-600',
    bgColor: 'bg-green-50',
    borderColor: 'border-green-200',
    iconBg: 'bg-green-600',
  },
  {
    id: 'security',
    title: 'Security Request',
    description: 'Access permissions, certificates, and security configs',
    icon: Shield,
    color: 'from-purple-500 to-purple-600',
    bgColor: 'bg-purple-50',
    borderColor: 'border-purple-200',
    iconBg: 'bg-purple-600',
  },
  {
    id: 'database',
    title: 'Database Request',
    description: 'Create databases, schemas, and manage access',
    icon: Database,
    color: 'from-orange-500 to-orange-600',
    bgColor: 'bg-orange-50',
    borderColor: 'border-orange-200',
    iconBg: 'bg-orange-600',
  },
  {
    id: 'support',
    title: 'Support Request',
    description: 'Report issues, get help, or request assistance',
    icon: HelpCircle,
    color: 'from-red-500 to-red-600',
    bgColor: 'bg-red-50',
    borderColor: 'border-red-200',
    iconBg: 'bg-red-600',
  },
  {
    id: 'custom',
    title: 'Custom Request',
    description: 'Create a custom request for specific needs',
    icon: Settings,
    color: 'from-teal-500 to-teal-600',
    bgColor: 'bg-teal-50',
    borderColor: 'border-teal-200',
    iconBg: 'bg-teal-600',
  },
];

const pipelineTemplates = [
  {
    id: 1,
    title: 'React App Pipeline',
    description: 'Build, test, and deploy React applications',
    icon: '⚛️',
    color: 'bg-blue-600',
  },
  {
    id: 2,
    title: 'Node.js API Pipeline',
    description: 'Maintain API development and testing',
    icon: '🟢',
    color: 'bg-green-600',
  },
  {
    id: 3,
    title: 'Docker Container Pipeline',
    description: 'Containerized application deployment',
    icon: '🐳',
    color: 'bg-blue-500',
  },
  {
    id: 4,
    title: 'Microservice Pipeline',
    description: 'Multi-service deployment workflow',
    icon: '🔄',
    color: 'bg-orange-600',
  },
];

const recentRequests = [
  {
    id: 1,
    title: 'Production Database Access',
    type: 'Database',
    status: 'approved',
    timeAgo: '2 hours ago',
    icon: '🗄️',
  },
  {
    id: 2,
    title: 'CI/CD Pipeline Setup',
    type: 'Pipeline',
    status: 'pending',
    timeAgo: '1 day ago',
    icon: '⚡',
  },
  {
    id: 3,
    title: 'AWS S3 Bucket Creation',
    type: 'Infrastructure',
    status: 'in_progress',
    timeAgo: '3 days ago',
    icon: '☁️',
  },
];

const statusConfig = {
  approved: { label: 'Approved', color: 'bg-green-100 text-green-700', icon: Check },
  pending: { label: 'Pending', color: 'bg-yellow-100 text-yellow-700', icon: Clock },
  in_progress: { label: 'In Progress', color: 'bg-blue-100 text-blue-700', icon: Clock },
  rejected: { label: 'Rejected', color: 'bg-red-100 text-red-700', icon: XCircle },
};

export default function RequestsPage() {
  return (
    <div className="min-h-screen bg-gray-50">
      {/* Hero Section */}
      <div className="bg-white border-b border-gray-200">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-12 text-center">
          <h1 className="text-4xl font-bold text-gray-900 mb-4">Requests</h1>
          <p className="text-lg text-gray-600 max-w-2xl mx-auto">
            Streamline your development workflow with automated requests, documentation, and DevOps integration.
          </p>
        </div>
      </div>

      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        {/* DevOps Requests Section */}
        <div className="mb-12">
          <div className="flex items-center justify-between mb-6">
            <div>
              <h2 className="text-2xl font-bold text-gray-900">DevOps Requests</h2>
              <p className="text-gray-600">Generate common requests or use pipeline templates</p>
            </div>
            <button className="px-6 py-3 bg-blue-600 text-white rounded-lg font-medium hover:bg-blue-700 shadow-lg">
              + New Request
            </button>
          </div>

          <div className="grid md:grid-cols-2 lg:grid-cols-3 gap-6">
            {requestTypes.map((type) => (
              <Card
                key={type.id}
                className={`${type.bgColor} border-2 ${type.borderColor} hover:shadow-xl transition-all cursor-pointer group`}
              >
                <div className="p-6">
                  <div className={`w-12 h-12 ${type.iconBg} rounded-xl flex items-center justify-center mb-4 group-hover:scale-110 transition-transform`}>
                    <type.icon className="w-6 h-6 text-white" />
                  </div>
                  <h3 className="text-lg font-bold text-gray-900 mb-2">{type.title}</h3>
                  <p className="text-sm text-gray-600 mb-4">{type.description}</p>
                  <button className="flex items-center gap-2 text-sm font-medium text-blue-600 hover:text-blue-700">
                    Create Request
                    <ArrowRight className="w-4 h-4" />
                  </button>
                </div>
              </Card>
            ))}
          </div>
        </div>

        {/* Pipeline Templates Section */}
        <div className="mb-12">
          <h2 className="text-2xl font-bold text-gray-900 mb-6">Pipeline Templates</h2>
          <div className="grid md:grid-cols-2 lg:grid-cols-4 gap-4">
            {pipelineTemplates.map((template) => (
              <Card
                key={template.id}
                className="bg-white hover:shadow-lg transition-shadow cursor-pointer group"
              >
                <div className="p-6">
                  <div className="flex items-center justify-between mb-3">
                    <span className="text-3xl">{template.icon}</span>
                    <ArrowRight className="w-5 h-5 text-gray-400 group-hover:text-blue-600 group-hover:translate-x-1 transition-all" />
                  </div>
                  <h3 className="font-bold text-gray-900 mb-1">{template.title}</h3>
                  <p className="text-xs text-gray-600">{template.description}</p>
                </div>
              </Card>
            ))}
          </div>
        </div>

        {/* Recent Requests Section */}
        <div>
          <h2 className="text-2xl font-bold text-gray-900 mb-6">Recent Requests</h2>
          <Card className="bg-white">
            <div className="divide-y divide-gray-200">
              {recentRequests.map((request) => {
                const StatusIcon = statusConfig[request.status as keyof typeof statusConfig].icon;
                return (
                  <div key={request.id} className="p-6 hover:bg-gray-50 transition-colors cursor-pointer">
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-4">
                        <div className="w-10 h-10 bg-gray-100 rounded-lg flex items-center justify-center text-xl">
                          {request.icon}
                        </div>
                        <div>
                          <h3 className="font-semibold text-gray-900">{request.title}</h3>
                          <p className="text-sm text-gray-600">Requested {request.timeAgo}</p>
                        </div>
                      </div>
                      <div className="flex items-center gap-4">
                        <Badge className={statusConfig[request.status as keyof typeof statusConfig].color}>
                          <StatusIcon className="w-3 h-3 mr-1" />
                          {statusConfig[request.status as keyof typeof statusConfig].label}
                        </Badge>
                        <ArrowRight className="w-5 h-5 text-gray-400" />
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          </Card>
        </div>
      </div>
    </div>
  );
}

