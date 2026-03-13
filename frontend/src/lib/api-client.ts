import axios, { AxiosInstance, AxiosError } from 'axios';

const API_BASE_URL =
  typeof window === 'undefined'
    ? process.env.NEXT_PUBLIC_API_BASE_URL || ''
    : (process.env.NEXT_PUBLIC_API_BASE_URL as string) || '';

class ApiClient {
  private client: AxiosInstance;
  private token: string | null = null;

  constructor() {
    this.client = axios.create({
      baseURL: `${API_BASE_URL}/api`,
      timeout: 30000,
      headers: {
        'Content-Type': 'application/json',
      },
    });

    // Request interceptor to add auth token
    this.client.interceptors.request.use(
      (config) => {
        if (this.token) {
          config.headers.Authorization = `Bearer ${this.token}`;
        }
        return config;
      },
      (error) => Promise.reject(error)
    );

    // Response interceptor for error handling
    this.client.interceptors.response.use(
      (response) => response,
      (error: AxiosError) => {
        if (error.response?.status === 401) {
          // Token expired or invalid
          this.clearToken();
          if (typeof window !== 'undefined') {
            window.location.href = '/login';
          }
        }
        return Promise.reject(error);
      }
    );
  }

  setToken(token: string) {
    this.token = token;
    if (typeof window !== 'undefined') {
      localStorage.setItem('auth_token', token);
    }
  }

  getToken(): string | null {
    if (!this.token && typeof window !== 'undefined') {
      this.token = localStorage.getItem('auth_token');
    }
    return this.token;
  }

  clearToken() {
    this.token = null;
    if (typeof window !== 'undefined') {
      localStorage.removeItem('auth_token');
      localStorage.removeItem('user_data');
    }
  }

  // Auth API
  async completeOAuthLoginFromMessage(data: { token: string; user: any }) {
    this.setToken(data.token);
    if (typeof window !== 'undefined') {
      localStorage.setItem('user_data', JSON.stringify(data.user));
    }
  }

  async logout(): Promise<void> {
    this.clearToken();
  }

  // Dashboard API
  async getDashboards(): Promise<any> {
    const response = await this.client.get('/dashboards');
    return response.data;
  }

  async getDefaultDashboard(): Promise<any> {
    const response = await this.client.get('/dashboards/default');
    return response.data;
  }

  async updateDashboard(id: string, data: any): Promise<any> {
    const response = await this.client.put(`/dashboards/${id}`, data);
    return response.data;
  }

  async getWidgetTypes(): Promise<any> {
    const response = await this.client.get('/dashboards/widget-types');
    return response.data;
  }

  // Azure DevOps API
  async getWorkItems(project?: string): Promise<any> {
    const response = await this.client.get('/azure-devops/work-items', {
      params: project ? { project } : {},
    });
    return response.data;
  }

  async getAzureDevOpsProjects(): Promise<{ success: boolean; data: { id: string; name: string }[] }> {
    const response = await this.client.get('/azure-devops/projects');
    return response.data;
  }

  async getPullRequests(username: string): Promise<any> {
    const response = await this.client.get('/azure-devops/pull-requests', {
      params: { username },
    });
    return response.data;
  }

  async getPipelines(): Promise<any> {
    const response = await this.client.get('/azure-devops/pipelines');
    return response.data;
  }

  async createProject(payload: {
    project_name: string;
    process_type: string;
    admin_username: string;
  }): Promise<{ success: boolean; data: { job_id: string; status: string } }> {
    const response = await this.client.post('/azure-devops/projects/create', payload);
    return response.data;
  }

  async getProjectCreationStatus(jobId: string): Promise<{
    success: boolean;
    data: {
      status: 'pending' | 'running' | 'succeeded' | 'failed' | 'error';
      project_url?: string;
      error?: string;
    };
  }> {
    const response = await this.client.get(
      `/azure-devops/projects/create/status/${jobId}`
    );
    return response.data;
  }

  // Azure DevOps PAT management
  async getAzureDevOpsPatStatus(): Promise<{ success: boolean; data: { configured: boolean; has_personal_pat: boolean } }> {
    const response = await this.client.get('/azure-devops/pat');
    return response.data;
  }

  async saveAzureDevOpsPat(pat: string): Promise<void> {
    await this.client.post('/azure-devops/pat', { pat });
  }

  async deleteAzureDevOpsPat(): Promise<void> {
    await this.client.delete('/azure-devops/pat');
  }

  // SonarQube API
  async getSonarProjects(): Promise<any> {
    const response = await this.client.get('/sonarqube/projects');
    return response.data;
  }

  async getSonarProject(key: string): Promise<any> {
    const response = await this.client.get(`/sonarqube/projects/${key}`);
    return response.data;
  }

  // Artifactory API
  async getArtifacts(): Promise<any> {
    const response = await this.client.get('/artifactory/artifacts');
    return response.data;
  }

  async getRepositories(): Promise<any> {
    const response = await this.client.get('/artifactory/repositories');
    return response.data;
  }

  async getStorageInfo(): Promise<any> {
    const response = await this.client.get('/artifactory/storage');
    return response.data;
  }

  // ServiceNow / Support API
  async getTickets(username?: string): Promise<any> {
    const response = await this.client.get('/support/tickets', {
      params: username ? { username } : {},
    });
    return response.data;
  }

  async getTicketDetail(sysId: string): Promise<any> {
    const response = await this.client.get(`/support/tickets/${sysId}`);
    return response.data;
  }

  async replyToTicket(sysId: string, message: string): Promise<any> {
    const response = await this.client.post(`/support/tickets/${sysId}/reply`, { message });
    return response.data;
  }

  async createSupportTicket(title: string, description: string): Promise<any> {
    const response = await this.client.post('/support/tickets', { title, description });
    return response.data;
  }

  async getIncidentStats(): Promise<any> {
    const response = await this.client.get('/support/stats');
    return response.data;
  }

  // AI Chatbot API
  async sendChatMessage(message: string, conversationId?: string): Promise<any> {
    const response = await this.client.post('/ai-chatbot/chat', {
      message,
      conversationId,
      userId: this.getUserId(),
    });
    return response.data;
  }

  async getConversations(): Promise<any> {
    const response = await this.client.get('/ai-chatbot/conversations', {
      params: { userId: this.getUserId() },
    });
    return response.data;
  }

  // Approval API
  async getApprovalRequests(status?: string): Promise<any> {
    const response = await this.client.get('/approvals/requests', {
      params: status ? { status } : undefined,
    });
    return response.data;
  }

  async createApprovalRequest(data: any): Promise<any> {
    const response = await this.client.post('/approvals/requests', data);
    return response.data;
  }

  async approveRequest(id: string, comments?: string): Promise<any> {
    const response = await this.client.post(`/approvals/requests/${id}/approve`, {
      comments,
    });
    return response.data;
  }

  async rejectRequest(id: string, comments: string): Promise<any> {
    const response = await this.client.post(`/approvals/requests/${id}/reject`, {
      comments,
    });
    return response.data;
  }

  // Metrics API
  async getUsageMetrics(period?: string): Promise<any> {
    const response = await this.client.get('/metrics/usage', {
      params: { period },
    });
    return response.data;
  }

  async getServiceMetrics(): Promise<any> {
    const response = await this.client.get('/metrics/services');
    return response.data;
  }

  async getApprovalMetrics(): Promise<any> {
    const response = await this.client.get('/metrics/approvals');
    return response.data;
  }

  // Health check
  async healthCheck(): Promise<any> {
    const response = await this.client.get('/health');
    return response.data;
  }

  private getUserId(): string {
    if (typeof window !== 'undefined') {
      const userData = localStorage.getItem('user_data');
      if (userData) {
        return JSON.parse(userData).id;
      }
    }
    return '';
  }
}

export const apiClient = new ApiClient();

