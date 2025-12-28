'use client';

import { AzureDevOpsWorkItems } from './AzureDevOpsWorkItems';
import { SonarQubeQualityGate } from './SonarQubeQualityGate';
import { ServiceNowTickets } from './ServiceNowTickets';

interface WidgetConfig {
  id: string;
  widget_key: string;
  config?: Record<string, any>;
}

interface WidgetRendererProps {
  widget: WidgetConfig;
}

export function WidgetRenderer({ widget }: WidgetRendererProps) {
  // Map widget keys to components
  const widgetComponents: Record<string, React.ComponentType<any>> = {
    ado_my_work_items: AzureDevOpsWorkItems,
    sonar_quality_gate: SonarQubeQualityGate,
    snow_my_tickets: ServiceNowTickets,
    // Add more widgets as needed
  };

  const Component = widgetComponents[widget.widget_key];

  if (!Component) {
    return (
      <div className="h-full flex items-center justify-center bg-gray-50 rounded-lg border-2 border-dashed border-gray-300">
        <p className="text-secondary-500 text-sm">Widget not found: {widget.widget_key}</p>
      </div>
    );
  }

  return <Component config={widget.config} />;
}

