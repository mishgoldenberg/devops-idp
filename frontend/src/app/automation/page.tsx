'use client';

import { Card } from '@/components/common/Card';
import { Badge } from '@/components/common/Badge';
import { 
  Zap, Bell, GitPullRequest, Bug, Users, Play, Settings, TrendingUp
} from 'lucide-react';

const automations = [
  {
    id: 1,
    title: 'ADO Mention Notifications',
    description: "Automatic email notifications when you're mentioned in Azure DevOps work items, pull requests, or comments.",
    icon: Bell,
    iconBg: 'bg-blue-600',
    status: 'active',
    users: 124,
    badge: 'Configure',
  },
  {
    id: 2,
    title: 'PR Status Updates',
    description: 'Get instant notifications about pull request approvals, comments, and merge status updates.',
    icon: GitPullRequest,
    iconBg: 'bg-green-600',
    status: 'active',
    users: 89,
    badge: 'Configure',
  },
  {
    id: 3,
    title: 'Bug Report Automation',
    description: 'Automatically create and assign bug reports based on error logs and monitoring alerts.',
    icon: Bug,
    iconBg: 'bg-orange-600',
    status: 'beta',
    users: 45,
    badge: 'Try Beta',
  },
];

const metrics = [
  { label: 'Active Automations', value: '24', trend: '+12%', icon: Zap, color: 'text-blue-600' },
  { label: 'Active Users', value: '156', trend: '+8%', icon: Users, color: 'text-green-600' },
  { label: 'Open Requests', value: '89', trend: '+23%', icon: GitPullRequest, color: 'text-purple-600' },
  { label: 'Avg Response Time', value: '2.3h', trend: '-15%', icon: TrendingUp, color: 'text-orange-600' },
];

const statusColors = {
  active: 'bg-green-100 text-green-700',
  beta: 'bg-yellow-100 text-yellow-700',
  inactive: 'bg-gray-100 text-gray-700',
};

const recentActivity = [
  { user: 'Sarah', action: 'enabled ADO Notifications', time: '2 hours ago', avatar: '👩' },
  { user: 'Mike', action: 'requested Slack Integration', time: '5 hours ago', avatar: '👨' },
  { user: 'Lisa', action: 'published new How-To Guide', time: '1 day ago', avatar: '👩‍💼' },
];

export default function AutomationPage() {
  return (
    <div className="min-h-screen bg-gray-50">
      {/* Hero Section */}
      <div className="bg-gradient-to-r from-blue-600 to-purple-600 text-white">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-16 text-center">
          <h1 className="text-4xl font-bold mb-4">Welcome to DevOps Portal</h1>
          <p className="text-xl text-blue-100 max-w-3xl mx-auto mb-8">
            Your central hub for development resources, automation tools, and streamlined workflows. 
            Empower your team with intelligent solutions.
          </p>
          <div className="flex items-center justify-center gap-4">
            <button className="px-8 py-3 bg-white text-blue-600 rounded-lg font-semibold hover:bg-blue-50 shadow-lg">
              Get Started
            </button>
            <button className="px-8 py-3 bg-blue-700 text-white rounded-lg font-semibold hover:bg-blue-800 border border-blue-400">
              Learn More
            </button>
          </div>
        </div>
      </div>

      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        {/* Metrics Cards */}
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6 mb-12">
          {metrics.map((metric, index) => (
            <Card key={index} className="bg-white hover:shadow-lg transition-shadow">
              <div className="p-6">
                <div className="flex items-center justify-between mb-4">
                  <metric.icon className={`w-8 h-8 ${metric.color}`} />
                  <span className={`text-sm font-semibold ${metric.trend.startsWith('+') ? 'text-green-600' : 'text-red-600'}`}>
                    {metric.trend}
                  </span>
                </div>
                <div className="text-3xl font-bold text-gray-900 mb-1">{metric.value}</div>
                <div className="text-sm text-gray-600">{metric.label}</div>
              </div>
            </Card>
          ))}
        </div>

        {/* Featured Automations Section */}
        <div className="mb-12">
          <div className="flex items-center justify-between mb-6">
            <div>
              <h2 className="text-2xl font-bold text-gray-900">Featured Automations</h2>
              <p className="text-gray-600">Streamline your workflow with popular automations</p>
            </div>
            <a href="#" className="text-blue-600 hover:text-blue-700 font-medium">View All →</a>
          </div>

          <div className="grid md:grid-cols-3 gap-6">
            {automations.map((automation) => (
              <Card key={automation.id} className="bg-white hover:shadow-xl transition-all">
                <div className="p-6">
                  <div className="flex items-start justify-between mb-4">
                    <div className={`w-12 h-12 ${automation.iconBg} rounded-xl flex items-center justify-center`}>
                      <automation.icon className="w-6 h-6 text-white" />
                    </div>
                    <Badge className={statusColors[automation.status as keyof typeof statusColors]}>
                      {automation.status}
                    </Badge>
                  </div>
                  <h3 className="text-lg font-bold text-gray-900 mb-2">{automation.title}</h3>
                  <p className="text-sm text-gray-600 mb-4">{automation.description}</p>
                  <div className="flex items-center justify-between pt-4 border-t border-gray-200">
                    <span className="text-xs text-gray-600">{automation.users} users</span>
                    <button className="text-sm font-medium text-blue-600 hover:text-blue-700">
                      {automation.badge}
                    </button>
                  </div>
                </div>
              </Card>
            ))}
          </div>
        </div>

        {/* Quick Actions */}
        <div className="mb-12">
          <h2 className="text-2xl font-bold text-gray-900 mb-6">Quick Actions</h2>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            {[
              { icon: '🎫', label: 'New Ticket', color: 'from-blue-500 to-blue-600' },
              { icon: '📦', label: 'Upload Artifact', color: 'from-purple-500 to-purple-600' },
              { icon: '▶️', label: 'Run Pipeline', color: 'from-green-500 to-green-600' },
              { icon: '📚', label: 'View Docs', color: 'from-orange-500 to-orange-600' },
            ].map((action, index) => (
              <button
                key={index}
                className={`p-6 bg-gradient-to-br ${action.color} text-white rounded-xl hover:shadow-lg transition-all group`}
              >
                <div className="text-4xl mb-2 group-hover:scale-110 transition-transform">{action.icon}</div>
                <div className="font-semibold">{action.label}</div>
              </button>
            ))}
          </div>
        </div>

        {/* Recent Activity */}
        <div>
          <h2 className="text-2xl font-bold text-gray-900 mb-6">Recent Activity</h2>
          <Card className="bg-white">
            <div className="divide-y divide-gray-200">
              {recentActivity.map((activity, index) => (
                <div key={index} className="p-4 hover:bg-gray-50 transition-colors">
                  <div className="flex items-center gap-4">
                    <div className="w-10 h-10 bg-gradient-to-br from-blue-500 to-purple-600 rounded-full flex items-center justify-center text-xl">
                      {activity.avatar}
                    </div>
                    <div className="flex-1">
                      <p className="text-sm">
                        <span className="font-semibold text-gray-900">{activity.user}</span>
                        <span className="text-gray-600"> {activity.action}</span>
                      </p>
                      <p className="text-xs text-gray-500">{activity.time}</p>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </Card>
        </div>

        {/* Request Automation CTA */}
        <div className="mt-12 bg-gradient-to-r from-purple-600 to-pink-600 rounded-2xl p-8 text-center text-white">
          <Zap className="w-12 h-12 mx-auto mb-4" />
          <h2 className="text-2xl font-bold mb-2">Request New Automation</h2>
          <p className="text-purple-100 mb-6">Have an idea for a new automation? Let us know!</p>
          <button className="px-8 py-3 bg-white text-purple-600 rounded-lg font-semibold hover:bg-purple-50">
            + Request New Automation
          </button>
        </div>
      </div>
    </div>
  );
}

