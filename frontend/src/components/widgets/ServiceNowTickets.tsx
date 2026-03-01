'use client';

import { useEffect, useState } from 'react';
import { Card, CardHeader, CardBody } from '../common/Card';
import { Badge } from '../common/Badge';
import { Skeleton } from '../common/Skeleton';
import { apiClient } from '@/lib/api-client';
import { getUser } from '@/lib/auth';
import { getStatusColor } from '@/lib/utils';
import { Ticket } from 'lucide-react';

interface SnowTicket {
  number: string;
  short_description: string;
  state: string;
  priority: string;
  url: string;
}

export function ServiceNowTickets() {
  const [tickets, setTickets] = useState<SnowTicket[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchTickets();
  }, []);

  async function fetchTickets() {
    try {
      setLoading(true);
      const user = getUser();
      const response = await apiClient.getTickets(user?.username || '');
      
      if (response.success) {
        setTickets(response.data);
      }
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  if (loading) {
    return (
      <Card className="h-full">
        <CardHeader>
          <div className="flex items-center gap-2">
            <Ticket className="w-5 h-5 text-primary" />
            <h3 className="font-semibold">My Tickets</h3>
          </div>
        </CardHeader>
        <CardBody>
          <Skeleton className="h-20 w-full" />
        </CardBody>
      </Card>
    );
  }

  if (error) {
    return (
      <Card className="h-full">
        <CardHeader>
          <div className="flex items-center gap-2">
            <Ticket className="w-5 h-5 text-primary" />
            <h3 className="font-semibold">My Tickets</h3>
          </div>
        </CardHeader>
        <CardBody>
          <p className="text-error text-sm">Failed to load tickets</p>
        </CardBody>
      </Card>
    );
  }

  return (
    <Card className="h-full flex flex-col">
      <CardHeader>
        <div className="flex items-center justify-between w-full">
          <div className="flex items-center gap-2">
            <Ticket className="w-5 h-5 text-primary" />
            <h3 className="font-semibold">My Tickets</h3>
          </div>
          <Badge variant="info">{tickets.length}</Badge>
        </div>
      </CardHeader>
      <CardBody className="flex-1 overflow-auto">
        {tickets.length > 0 ? (
          <div className="space-y-3">
            {tickets.map(ticket => (
              <a
                key={ticket.number}
                href={ticket.url}
                target="_blank"
                rel="noopener noreferrer"
                className="block p-3 border border-gray-200 dark:border-gray-700 rounded-lg hover:border-primary dark:hover:border-primary-600 hover:bg-primary-50 dark:hover:bg-primary-900/30 transition-colors"
              >
                <div className="flex items-start justify-between gap-2 mb-2">
                  <span className="text-sm font-medium text-primary dark:text-primary-400">
                    {ticket.number}
                  </span>
                  <Badge variant={getStatusColor(ticket.state) as any} className="text-xs">
                    {ticket.state}
                  </Badge>
                </div>
                <p className="text-sm text-gray-900 dark:text-gray-100 line-clamp-2 mb-2">
                  {ticket.short_description}
                </p>
                <span className="text-xs text-secondary-500 dark:text-secondary-400">{ticket.priority}</span>
              </a>
            ))}
          </div>
        ) : (
          <div className="text-center py-8 text-secondary-500 dark:text-secondary-400">
            <Ticket className="w-12 h-12 mx-auto mb-2 opacity-30" />
            <p>No open tickets</p>
          </div>
        )}
      </CardBody>
    </Card>
  );
}

