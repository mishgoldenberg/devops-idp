'use client';

import { Card } from '@/components/common/Card';
import { Badge } from '@/components/common/Badge';
import { FileText } from 'lucide-react';

const tickets = [
  { id: '#1234', title: 'Database Migration', status: 'in_progress', opened: '2 days ago', priority: 'high' },
  { id: '#1233', title: 'SSL Certificate Renewal', status: 'resolved', opened: '5 days ago', priority: 'medium' },
  { id: '#1232', title: 'Server Scaling Request', status: 'urgent', opened: '1 week ago', priority: 'urgent' },
];

const statusColors = {
  in_progress: { bg: 'bg-yellow-100 dark:bg-yellow-900/30', text: 'text-yellow-700 dark:text-yellow-300', label: 'In Progress' },
  resolved: { bg: 'bg-green-100 dark:bg-green-900/30', text: 'text-green-700 dark:text-green-300', label: 'Resolved' },
  urgent: { bg: 'bg-red-100 dark:bg-red-900/30', text: 'text-red-700 dark:text-red-300', label: 'Urgent' },
};

export function DevOpsTicketsWidget() {
  return (
    <Card className="h-full">
      <div className="p-6">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <div className="w-8 h-8 bg-red-100 dark:bg-red-900/30 rounded-lg flex items-center justify-center">
              <FileText className="w-4 h-4 text-red-600 dark:text-red-400" />
            </div>
            <h2 className="text-lg font-bold text-gray-900 dark:text-gray-100">DevOps Tickets</h2>
          </div>
          <a href="#" className="text-sm font-medium text-blue-600 dark:text-blue-400 hover:text-blue-700 dark:hover:text-blue-300">View All</a>
        </div>
        <div className="space-y-3">
          {tickets.map((ticket) => (
            <div key={ticket.id} className="p-4 bg-gray-50 dark:bg-gray-700/50 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors cursor-pointer">
              <div className="flex items-start justify-between mb-2">
                <div className="flex-1">
                  <div className="flex items-center gap-2 mb-1">
                    <span className="font-semibold text-gray-900 dark:text-gray-100">{ticket.title}</span>
                    <span className="text-xs text-gray-500 dark:text-gray-400">{ticket.id}</span>
                  </div>
                  <p className="text-xs text-gray-600 dark:text-gray-400">Opened {ticket.opened}</p>
                </div>
                <Badge className={`${statusColors[ticket.status as keyof typeof statusColors].bg} ${statusColors[ticket.status as keyof typeof statusColors].text}`}>
                  {statusColors[ticket.status as keyof typeof statusColors].label}
                </Badge>
              </div>
            </div>
          ))}
        </div>
      </div>
    </Card>
  );
}

