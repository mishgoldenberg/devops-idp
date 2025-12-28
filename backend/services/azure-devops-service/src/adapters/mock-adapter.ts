// Mock adapter for Azure DevOps (for development/testing)

import { WorkItem, PullRequest, Pipeline } from '@devops-control-center/shared';

export class MockAzureDevOpsAdapter {
  /**
   * Get work items for user
   */
  async getWorkItems(username: string): Promise<WorkItem[]> {
    // Simulate API delay
    await this.delay(200);

    return [
      {
        id: 1001,
        title: 'Implement user authentication',
        state: 'In Progress',
        type: 'User Story',
        assigned_to: username,
        created_date: new Date('2024-01-10'),
        changed_date: new Date('2024-01-15'),
        url: 'https://dev.azure.com/org/project/_workitems/edit/1001',
      },
      {
        id: 1002,
        title: 'Fix dashboard loading performance',
        state: 'To Do',
        type: 'Bug',
        assigned_to: username,
        created_date: new Date('2024-01-12'),
        changed_date: new Date('2024-01-12'),
        url: 'https://dev.azure.com/org/project/_workitems/edit/1002',
      },
      {
        id: 1003,
        title: 'Add widget customization',
        state: 'To Do',
        type: 'Feature',
        assigned_to: username,
        created_date: new Date('2024-01-13'),
        changed_date: new Date('2024-01-13'),
        url: 'https://dev.azure.com/org/project/_workitems/edit/1003',
      },
    ];
  }

  /**
   * Get pull requests for user
   */
  async getPullRequests(username: string): Promise<PullRequest[]> {
    await this.delay(150);

    return [
      {
        id: 2001,
        title: 'feat: Add RBAC middleware',
        status: 'Active',
        created_by: username,
        created_date: new Date('2024-01-14'),
        repository: 'backend',
        source_branch: 'feature/rbac',
        target_branch: 'main',
        url: 'https://dev.azure.com/org/project/_git/backend/pullrequest/2001',
        reviewers: [
          { name: 'John Reviewer', vote: 10 }, // Approved
          { name: 'Jane Reviewer', vote: 0 },  // No vote
        ],
      },
      {
        id: 2002,
        title: 'fix: Correct dashboard layout bug',
        status: 'Active',
        created_by: username,
        created_date: new Date('2024-01-15'),
        repository: 'frontend',
        source_branch: 'bugfix/dashboard-layout',
        target_branch: 'main',
        url: 'https://dev.azure.com/org/project/_git/frontend/pullrequest/2002',
        reviewers: [
          { name: 'Bob Reviewer', vote: 5 }, // Approved with suggestions
        ],
      },
    ];
  }

  /**
   * Get pipeline runs
   */
  async getPipelineRuns(projectName?: string): Promise<Pipeline[]> {
    await this.delay(180);

    return [
      {
        id: 101,
        name: 'CI Pipeline',
        run_id: 5001,
        status: 'completed',
        result: 'succeeded',
        created_date: new Date('2024-01-15T10:30:00'),
        finished_date: new Date('2024-01-15T10:45:00'),
        url: 'https://dev.azure.com/org/project/_build/results?buildId=5001',
      },
      {
        id: 102,
        name: 'CD Pipeline',
        run_id: 5002,
        status: 'inProgress',
        created_date: new Date('2024-01-15T11:00:00'),
        url: 'https://dev.azure.com/org/project/_build/results?buildId=5002',
      },
      {
        id: 101,
        name: 'CI Pipeline',
        run_id: 5003,
        status: 'completed',
        result: 'failed',
        created_date: new Date('2024-01-14T15:20:00'),
        finished_date: new Date('2024-01-14T15:28:00'),
        url: 'https://dev.azure.com/org/project/_build/results?buildId=5003',
      },
    ];
  }

  /**
   * Create new Azure DevOps project
   */
  async createProject(projectData: any): Promise<any> {
    await this.delay(300);

    // Mock project creation
    return {
      id: 'mock-project-' + Date.now(),
      name: projectData.name,
      description: projectData.description,
      url: `https://dev.azure.com/org/${projectData.name}`,
      created: new Date(),
    };
  }

  private delay(ms: number): Promise<void> {
    return new Promise(resolve => setTimeout(resolve, ms));
  }
}

