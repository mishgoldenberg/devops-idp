'use client';

import { useState, useEffect, useRef, useCallback } from 'react';
import { useRouter } from 'next/navigation';
import { isAuthenticated, getUser } from '@/lib/auth';
import { apiClient } from '@/lib/api-client';
import { Card, CardHeader, CardBody } from '@/components/common/Card';
import { Badge } from '@/components/common/Badge';
import { Skeleton } from '@/components/common/Skeleton';
import {
  Ticket,
  Plus,
  X,
  Send,
  RefreshCw,
  AlertCircle,
  MessageSquare,
  ChevronRight,
  Clock,
  User,
} from 'lucide-react';
import { cn } from '@/lib/utils';

// ── Types ─────────────────────────────────────────────────────────────────────

interface SupportTicket {
  sys_id: string;
  number: string;
  short_description: string;
  state: string;
  priority: string;
  assigned_to: string;
  opened_at: string;
  description?: string;
  conversation?: ConversationMessage[];
}

interface ConversationMessage {
  sys_id: string;
  sys_created_by: string;
  sys_created_on: string;
  value: string;
}

// ── State color helpers ────────────────────────────────────────────────────────

const STATE_COLORS: Record<string, string> = {
  'New': 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300',
  'In Progress': 'bg-yellow-100 text-yellow-700 dark:bg-yellow-900/40 dark:text-yellow-300',
  'On Hold': 'bg-orange-100 text-orange-700 dark:bg-orange-900/40 dark:text-orange-300',
  'Resolved': 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300',
  'Closed': 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-400',
  'Cancelled': 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300',
};

function stateColor(state: string) {
  return STATE_COLORS[state] ?? 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-400';
}

function formatDate(iso: string) {
  if (!iso) return '';
  try {
    return new Date(iso).toLocaleString(undefined, {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  } catch {
    return iso;
  }
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function SupportPage() {
  const router = useRouter();
  const currentUser = getUser();

  // ── Tickets list state ──────────────────────────────────────────────────────
  const [tickets, setTickets] = useState<SupportTicket[]>([]);
  const [ticketsLoading, setTicketsLoading] = useState(true);
  const [ticketsError, setTicketsError] = useState<string | null>(null);

  // ── Selected ticket state ───────────────────────────────────────────────────
  const [selectedTicket, setSelectedTicket] = useState<SupportTicket | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);

  // ── Reply state ─────────────────────────────────────────────────────────────
  const [replyText, setReplyText] = useState('');
  const [replySending, setReplySending] = useState(false);
  const [replyError, setReplyError] = useState<string | null>(null);

  // ── Create ticket modal state ───────────────────────────────────────────────
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [createTitle, setCreateTitle] = useState('');
  const [createDescription, setCreateDescription] = useState('');
  const [createLoading, setCreateLoading] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  const conversationEndRef = useRef<HTMLDivElement>(null);

  // ── Auth guard ──────────────────────────────────────────────────────────────
  useEffect(() => {
    if (!isAuthenticated()) router.push('/login');
  }, [router]);

  if (!isAuthenticated()) return null;

  // ── Data fetching ────────────────────────────────────────────────────────────

  // eslint-disable-next-line react-hooks/rules-of-hooks
  const fetchTickets = useCallback(async () => {
    setTicketsLoading(true);
    setTicketsError(null);
    try {
      const res = await apiClient.getTickets();
      if (res.success) setTickets(res.data);
    } catch {
      setTicketsError('Could not load tickets. ServiceNow may be unreachable.');
    } finally {
      setTicketsLoading(false);
    }
  }, []);

  // eslint-disable-next-line react-hooks/rules-of-hooks
  useEffect(() => { fetchTickets(); }, [fetchTickets]);

  // eslint-disable-next-line react-hooks/rules-of-hooks
  useEffect(() => {
    if (conversationEndRef.current) {
      conversationEndRef.current.scrollIntoView({ behavior: 'smooth' });
    }
  }, [selectedTicket?.conversation]);

  async function openTicket(ticket: SupportTicket) {
    setSelectedTicket({ ...ticket, conversation: undefined });
    setDetailLoading(true);
    setDetailError(null);
    setReplyText('');
    setReplyError(null);
    try {
      const res = await apiClient.getTicketDetail(ticket.sys_id);
      if (res.success) setSelectedTicket(res.data);
    } catch {
      setDetailError('Could not load ticket details.');
    } finally {
      setDetailLoading(false);
    }
  }

  async function handleSendReply() {
    if (!selectedTicket || !replyText.trim()) return;
    setReplySending(true);
    setReplyError(null);
    try {
      const res = await apiClient.replyToTicket(selectedTicket.sys_id, replyText.trim());
      if (res.success) {
        setSelectedTicket((prev) =>
          prev ? { ...prev, conversation: [...(prev.conversation ?? []), res.data] } : prev
        );
        setReplyText('');
      }
    } catch {
      setReplyError('Failed to send reply. Please try again.');
    } finally {
      setReplySending(false);
    }
  }

  async function handleCreateTicket() {
    if (!createTitle.trim() || !createDescription.trim()) return;
    setCreateLoading(true);
    setCreateError(null);
    try {
      const res = await apiClient.createSupportTicket(createTitle.trim(), createDescription.trim());
      if (res.success) {
        setShowCreateModal(false);
        setCreateTitle('');
        setCreateDescription('');
        await fetchTickets();
        openTicket(res.data);
      }
    } catch {
      setCreateError('Failed to create ticket. Please try again.');
    } finally {
      setCreateLoading(false);
    }
  }

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <div className="min-h-screen bg-gray-50 dark:bg-gray-900/50">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8 space-y-6">

        {/* Page header */}
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-3xl font-bold text-gray-900 dark:text-white">Support</h1>
            <p className="mt-1 text-gray-600 dark:text-gray-400">
              View and manage your ServiceNow incidents
            </p>
          </div>
          <button
            onClick={() => setShowCreateModal(true)}
            className="inline-flex items-center gap-2 px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white text-sm font-semibold rounded-lg transition-colors"
          >
            <Plus className="w-4 h-4" />
            Create Ticket
          </button>
        </div>

        {/* Main two-column layout */}
        <div className="grid grid-cols-1 lg:grid-cols-5 gap-6 min-h-[600px]">

          {/* ── Left: Ticket list ─────────────────────────────────────────── */}
          <div className="lg:col-span-2">
            <Card className="bg-white dark:bg-gray-800 border-0 shadow-sm h-full flex flex-col">
              <CardHeader className="border-b border-gray-100 dark:border-gray-700">
                <div className="flex items-center justify-between w-full">
                  <div className="flex items-center gap-2">
                    <Ticket className="w-5 h-5 text-blue-600 dark:text-blue-400" />
                    <span className="font-semibold text-gray-900 dark:text-white">My Tickets</span>
                  </div>
                  <div className="flex items-center gap-2">
                    {!ticketsLoading && (
                      <Badge className="bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300">
                        {tickets.length}
                      </Badge>
                    )}
                    <button
                      onClick={fetchTickets}
                      disabled={ticketsLoading}
                      className="p-1.5 text-gray-500 hover:text-gray-900 dark:hover:text-gray-100 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors"
                      title="Refresh tickets"
                    >
                      <RefreshCw className={cn('w-4 h-4', ticketsLoading && 'animate-spin')} />
                    </button>
                  </div>
                </div>
              </CardHeader>

              <CardBody className="flex-1 overflow-y-auto p-0">
                {ticketsLoading ? (
                  <div className="p-4 space-y-3">
                    {[1, 2, 3].map((i) => (
                      <div key={i} className="space-y-2 p-3 border border-gray-100 dark:border-gray-700 rounded-lg">
                        <Skeleton className="h-4 w-24" />
                        <Skeleton className="h-4 w-full" />
                        <Skeleton className="h-3 w-20" />
                      </div>
                    ))}
                    <p className="text-center text-sm text-gray-500 dark:text-gray-400 pt-2">
                      Loading tickets…
                    </p>
                  </div>
                ) : ticketsError ? (
                  <div className="p-6 flex flex-col items-center gap-3 text-center">
                    <AlertCircle className="w-8 h-8 text-red-500" />
                    <p className="text-sm text-red-600 dark:text-red-400">{ticketsError}</p>
                    <button
                      onClick={fetchTickets}
                      className="text-sm text-blue-600 dark:text-blue-400 underline hover:no-underline"
                    >
                      Try again
                    </button>
                  </div>
                ) : tickets.length === 0 ? (
                  <div className="p-8 flex flex-col items-center gap-3 text-center">
                    <Ticket className="w-12 h-12 text-gray-300 dark:text-gray-600" />
                    <p className="text-sm text-gray-500 dark:text-gray-400">No tickets assigned to you</p>
                    <button
                      onClick={() => setShowCreateModal(true)}
                      className="text-sm text-blue-600 dark:text-blue-400 underline hover:no-underline"
                    >
                      Create your first ticket
                    </button>
                  </div>
                ) : (
                  <ul className="divide-y divide-gray-100 dark:divide-gray-700">
                    {tickets.map((ticket) => (
                      <li key={ticket.sys_id}>
                        <button
                          onClick={() => openTicket(ticket)}
                          className={cn(
                            'w-full text-left px-4 py-3.5 hover:bg-gray-50 dark:hover:bg-gray-700/50 transition-colors group',
                            selectedTicket?.sys_id === ticket.sys_id &&
                              'bg-blue-50 dark:bg-blue-900/20 border-l-2 border-blue-600'
                          )}
                        >
                          <div className="flex items-start justify-between gap-2 mb-1">
                            <span className="text-xs font-semibold text-blue-600 dark:text-blue-400 font-mono">
                              {ticket.number}
                            </span>
                            <span className={cn('text-xs px-2 py-0.5 rounded-full font-medium shrink-0', stateColor(ticket.state))}>
                              {ticket.state}
                            </span>
                          </div>
                          <p className="text-sm text-gray-900 dark:text-gray-100 line-clamp-2 group-hover:text-blue-700 dark:group-hover:text-blue-300 transition-colors">
                            {ticket.short_description}
                          </p>
                          <div className="flex items-center gap-1 mt-1.5">
                            <Clock className="w-3 h-3 text-gray-400" />
                            <span className="text-xs text-gray-500 dark:text-gray-400">
                              {formatDate(ticket.opened_at)}
                            </span>
                          </div>
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </CardBody>
            </Card>
          </div>

          {/* ── Right: Ticket detail + conversation ───────────────────────── */}
          <div className="lg:col-span-3">
            {!selectedTicket ? (
              <Card className="bg-white dark:bg-gray-800 border-0 shadow-sm h-full flex items-center justify-center">
                <div className="flex flex-col items-center gap-4 text-center p-8">
                  <MessageSquare className="w-14 h-14 text-gray-300 dark:text-gray-600" />
                  <div>
                    <p className="text-lg font-medium text-gray-700 dark:text-gray-300">
                      Select a ticket to view details
                    </p>
                    <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
                      Click any ticket from the list to open its conversation
                    </p>
                  </div>
                </div>
              </Card>
            ) : (
              <Card className="bg-white dark:bg-gray-800 border-0 shadow-sm h-full flex flex-col">

                {/* Ticket header */}
                <CardHeader className="border-b border-gray-100 dark:border-gray-700 shrink-0">
                  <div className="w-full space-y-2">
                    <div className="flex items-start justify-between gap-3">
                      <div className="flex items-center gap-2 min-w-0">
                        <span className="text-sm font-semibold text-blue-600 dark:text-blue-400 font-mono shrink-0">
                          {selectedTicket.number}
                        </span>
                        <ChevronRight className="w-3 h-3 text-gray-400 shrink-0" />
                        <span className="text-sm font-semibold text-gray-900 dark:text-white truncate">
                          {selectedTicket.short_description}
                        </span>
                      </div>
                      <span className={cn('text-xs px-2 py-1 rounded-full font-medium shrink-0', stateColor(selectedTicket.state))}>
                        {selectedTicket.state}
                      </span>
                    </div>
                    <div className="flex items-center gap-4 text-xs text-gray-500 dark:text-gray-400">
                      <span className="flex items-center gap-1">
                        <User className="w-3 h-3" />
                        {selectedTicket.assigned_to || 'Unassigned'}
                      </span>
                      <span className="flex items-center gap-1">
                        <Clock className="w-3 h-3" />
                        {formatDate(selectedTicket.opened_at)}
                      </span>
                    </div>
                    {selectedTicket.description && selectedTicket.description !== selectedTicket.short_description && (
                      <p className="text-sm text-gray-600 dark:text-gray-400 bg-gray-50 dark:bg-gray-700/50 rounded-lg px-3 py-2">
                        {selectedTicket.description}
                      </p>
                    )}
                  </div>
                </CardHeader>

                {/* Conversation */}
                <CardBody className="flex-1 overflow-y-auto space-y-4 min-h-0">
                  {detailLoading ? (
                    <div className="space-y-4">
                      {[1, 2].map((i) => (
                        <div key={i} className="flex gap-3">
                          <Skeleton className="w-8 h-8 rounded-full shrink-0" />
                          <div className="flex-1 space-y-2">
                            <Skeleton className="h-3 w-32" />
                            <Skeleton className="h-16 w-full rounded-lg" />
                          </div>
                        </div>
                      ))}
                      <p className="text-center text-sm text-gray-500 dark:text-gray-400">
                        Loading conversation…
                      </p>
                    </div>
                  ) : detailError ? (
                    <div className="flex flex-col items-center gap-3 py-8 text-center">
                      <AlertCircle className="w-8 h-8 text-red-500" />
                      <p className="text-sm text-red-600 dark:text-red-400">{detailError}</p>
                      <button
                        onClick={() => openTicket(selectedTicket)}
                        className="text-sm text-blue-600 dark:text-blue-400 underline hover:no-underline"
                      >
                        Retry
                      </button>
                    </div>
                  ) : !selectedTicket.conversation || selectedTicket.conversation.length === 0 ? (
                    <div className="flex flex-col items-center gap-3 py-8 text-center">
                      <MessageSquare className="w-10 h-10 text-gray-300 dark:text-gray-600" />
                      <p className="text-sm text-gray-500 dark:text-gray-400">No messages yet</p>
                    </div>
                  ) : (
                    <>
                      {selectedTicket.conversation.map((msg) => {
                        const isCurrentUser = msg.sys_created_by === currentUser?.username;
                        return (
                          <div
                            key={msg.sys_id}
                            className={cn(
                              'flex gap-3',
                              isCurrentUser ? 'flex-row-reverse' : 'flex-row'
                            )}
                          >
                            {/* Avatar */}
                            <div
                              className={cn(
                                'w-8 h-8 rounded-full flex items-center justify-center text-xs font-bold shrink-0',
                                isCurrentUser
                                  ? 'bg-blue-600 text-white'
                                  : 'bg-gray-200 dark:bg-gray-700 text-gray-700 dark:text-gray-300'
                              )}
                            >
                              {msg.sys_created_by.charAt(0).toUpperCase()}
                            </div>

                            {/* Message bubble */}
                            <div className={cn('flex-1 max-w-[80%]', isCurrentUser && 'items-end flex flex-col')}>
                              <div className="flex items-center gap-2 mb-1">
                                <span className={cn('text-xs font-medium', isCurrentUser ? 'text-blue-600 dark:text-blue-400' : 'text-gray-700 dark:text-gray-300')}>
                                  {msg.sys_created_by}
                                </span>
                                <span className="text-xs text-gray-400 dark:text-gray-500">
                                  {formatDate(msg.sys_created_on)}
                                </span>
                              </div>
                              <div
                                className={cn(
                                  'px-4 py-2.5 rounded-2xl text-sm leading-relaxed',
                                  isCurrentUser
                                    ? 'bg-blue-600 text-white rounded-tr-sm'
                                    : 'bg-gray-100 dark:bg-gray-700 text-gray-900 dark:text-gray-100 rounded-tl-sm'
                                )}
                              >
                                {msg.value}
                              </div>
                            </div>
                          </div>
                        );
                      })}
                      <div ref={conversationEndRef} />
                    </>
                  )}
                </CardBody>

                {/* Reply input */}
                <div className="border-t border-gray-100 dark:border-gray-700 p-4 shrink-0">
                  {replyError && (
                    <p className="text-xs text-red-600 dark:text-red-400 mb-2 flex items-center gap-1">
                      <AlertCircle className="w-3 h-3" />
                      {replyError}
                    </p>
                  )}
                  <div className="flex gap-2">
                    <textarea
                      value={replyText}
                      onChange={(e) => setReplyText(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter' && !e.shiftKey && !replySending) {
                          e.preventDefault();
                          handleSendReply();
                        }
                      }}
                      placeholder="Type a reply… (Enter to send, Shift+Enter for new line)"
                      rows={2}
                      disabled={replySending}
                      className="flex-1 resize-none rounded-xl border border-gray-200 dark:border-gray-600 bg-gray-50 dark:bg-gray-700 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 dark:focus:ring-blue-400 focus:border-transparent transition-colors disabled:opacity-50"
                    />
                    <button
                      onClick={handleSendReply}
                      disabled={!replyText.trim() || replySending}
                      className="px-4 py-2 bg-blue-600 hover:bg-blue-700 disabled:bg-gray-300 dark:disabled:bg-gray-600 text-white rounded-xl transition-colors flex items-center gap-2 shrink-0 self-end"
                      title="Send reply"
                    >
                      {replySending ? (
                        <RefreshCw className="w-4 h-4 animate-spin" />
                      ) : (
                        <Send className="w-4 h-4" />
                      )}
                      <span className="text-sm font-medium hidden sm:inline">
                        {replySending ? 'Sending…' : 'Send'}
                      </span>
                    </button>
                  </div>
                </div>
              </Card>
            )}
          </div>
        </div>
      </div>

      {/* ── Create Ticket Modal ─────────────────────────────────────────────── */}
      {showCreateModal && (
        <div className="fixed inset-0 bg-black/50 dark:bg-black/70 z-50 flex items-center justify-center p-4 backdrop-blur-sm">
          <Card className="w-full max-w-lg p-0 shadow-2xl bg-white dark:bg-gray-800">

            {/* Modal header */}
            <div className="bg-gradient-to-r from-blue-600 to-blue-700 px-6 py-5 flex items-center justify-between rounded-t-xl">
              <div className="flex items-center gap-3">
                <Plus className="w-5 h-5 text-white" />
                <h2 className="text-lg font-bold text-white">Create New Ticket</h2>
              </div>
              <button
                onClick={() => {
                  setShowCreateModal(false);
                  setCreateTitle('');
                  setCreateDescription('');
                  setCreateError(null);
                }}
                className="p-1.5 hover:bg-blue-500/30 rounded-lg transition-colors text-white"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            {/* Modal body */}
            <div className="p-6 space-y-5">
              {createError && (
                <div className="flex items-start gap-2 p-3 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg">
                  <AlertCircle className="w-4 h-4 text-red-600 dark:text-red-400 shrink-0 mt-0.5" />
                  <p className="text-sm text-red-700 dark:text-red-300">{createError}</p>
                </div>
              )}

              <div className="space-y-2">
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300">
                  Title <span className="text-red-500">*</span>
                </label>
                <input
                  type="text"
                  value={createTitle}
                  onChange={(e) => setCreateTitle(e.target.value)}
                  placeholder="Brief summary of the issue"
                  disabled={createLoading}
                  className="w-full rounded-lg border border-gray-200 dark:border-gray-600 bg-gray-50 dark:bg-gray-700 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent transition-colors disabled:opacity-50"
                />
              </div>

              <div className="space-y-2">
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300">
                  Description <span className="text-red-500">*</span>
                </label>
                <textarea
                  value={createDescription}
                  onChange={(e) => setCreateDescription(e.target.value)}
                  placeholder="Describe the issue in detail — include steps to reproduce, error messages, and any relevant context"
                  rows={5}
                  disabled={createLoading}
                  className="w-full resize-none rounded-lg border border-gray-200 dark:border-gray-600 bg-gray-50 dark:bg-gray-700 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent transition-colors disabled:opacity-50"
                />
              </div>

              <div className="flex gap-3 pt-2">
                <button
                  onClick={() => {
                    setShowCreateModal(false);
                    setCreateTitle('');
                    setCreateDescription('');
                    setCreateError(null);
                  }}
                  disabled={createLoading}
                  className="flex-1 px-4 py-2 border border-gray-200 dark:border-gray-600 text-gray-700 dark:text-gray-300 text-sm font-medium rounded-lg hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors disabled:opacity-50"
                >
                  Cancel
                </button>
                <button
                  onClick={handleCreateTicket}
                  disabled={createLoading || !createTitle.trim() || !createDescription.trim()}
                  className="flex-1 px-4 py-2 bg-blue-600 hover:bg-blue-700 disabled:bg-gray-300 dark:disabled:bg-gray-600 text-white text-sm font-semibold rounded-lg transition-colors flex items-center justify-center gap-2"
                >
                  {createLoading ? (
                    <>
                      <RefreshCw className="w-4 h-4 animate-spin" />
                      Creating…
                    </>
                  ) : (
                    <>
                      <Ticket className="w-4 h-4" />
                      Create Ticket
                    </>
                  )}
                </button>
              </div>
            </div>
          </Card>
        </div>
      )}
    </div>
  );
}
