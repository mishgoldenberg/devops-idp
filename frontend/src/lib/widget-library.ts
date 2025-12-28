import { DevOpsTicketsWidget } from '@/components/widgets/DevOpsTicketsWidget';
import { ArtifactoryWidget } from '@/components/widgets/ArtifactoryWidget';
import { SonarQubeWidget } from '@/components/widgets/SonarQubeWidget';
import { LatestPRWidget } from '@/components/widgets/LatestPRWidget';
import { PipelineWidget } from '@/components/widgets/PipelineWidget';
import { AzureDevOpsWorkItems } from '@/components/widgets/AzureDevOpsWorkItems';
import { ServiceNowTickets } from '@/components/widgets/ServiceNowTickets';
import { SonarQubeQualityGate } from '@/components/widgets/SonarQubeQualityGate';

export interface WidgetDefinition {
  id: string;
  name: string;
  description: string;
  component: React.ComponentType;
  category: 'devops' | 'quality' | 'integration' | 'monitoring';
  minWidth: number;
  minHeight: number;
  defaultWidth: number;
  defaultHeight: number;
  permissions?: string[]; // Required roles/permissions
}

export const WIDGET_LIBRARY: Record<string, WidgetDefinition> = {
  'devops-tickets': {
    id: 'devops-tickets',
    name: 'DevOps Tickets',
    description: 'View your assigned tickets and their status',
    component: DevOpsTicketsWidget,
    category: 'devops',
    minWidth: 1,
    minHeight: 2,
    defaultWidth: 1,
    defaultHeight: 2,
  },
  'artifactory': {
    id: 'artifactory',
    name: 'Artifactory Repositories',
    description: 'Monitor artifact repositories and storage',
    component: ArtifactoryWidget,
    category: 'devops',
    minWidth: 1,
    minHeight: 2,
    defaultWidth: 1,
    defaultHeight: 2,
  },
  'sonarqube-scan': {
    id: 'sonarqube-scan',
    name: 'SonarQube Latest Scan',
    description: 'Quality gate status and code metrics',
    component: SonarQubeWidget,
    category: 'quality',
    minWidth: 1,
    minHeight: 2,
    defaultWidth: 1,
    defaultHeight: 2,
  },
  'latest-pr': {
    id: 'latest-pr',
    name: 'Latest Pull Request',
    description: 'Your most recent pull request status',
    component: LatestPRWidget,
    category: 'devops',
    minWidth: 1,
    minHeight: 2,
    defaultWidth: 1,
    defaultHeight: 2,
  },
  'pipeline': {
    id: 'pipeline',
    name: 'Latest Pipeline',
    description: 'Most recent pipeline build status',
    component: PipelineWidget,
    category: 'devops',
    minWidth: 1,
    minHeight: 2,
    defaultWidth: 1,
    defaultHeight: 2,
  },
  'azure-workitems': {
    id: 'azure-workitems',
    name: 'Azure DevOps Work Items',
    description: 'Your assigned Azure DevOps work items',
    component: AzureDevOpsWorkItems,
    category: 'integration',
    minWidth: 1,
    minHeight: 2,
    defaultWidth: 1,
    defaultHeight: 2,
  },
  'servicenow-tickets': {
    id: 'servicenow-tickets',
    name: 'ServiceNow Tickets',
    description: 'Recent ServiceNow incidents and requests',
    component: ServiceNowTickets,
    category: 'integration',
    minWidth: 1,
    minHeight: 2,
    defaultWidth: 1,
    defaultHeight: 2,
  },
  'sonarqube-quality': {
    id: 'sonarqube-quality',
    name: 'SonarQube Quality Gate',
    description: 'Detailed code quality metrics',
    component: SonarQubeQualityGate,
    category: 'quality',
    minWidth: 1,
    minHeight: 2,
    defaultWidth: 1,
    defaultHeight: 2,
  },
};

export function getAvailableWidgets(userRole?: string): WidgetDefinition[] {
  return Object.values(WIDGET_LIBRARY).filter((widget) => {
    if (!widget.permissions || widget.permissions.length === 0) {
      return true;
    }
    return widget.permissions.some((permission) => {
      // Check if user has required permission
      // For now, all widgets are available to all roles
      return true;
    });
  });
}

export function getWidgetById(id: string): WidgetDefinition | undefined {
  return WIDGET_LIBRARY[id];
}

