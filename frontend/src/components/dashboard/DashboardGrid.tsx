'use client';

import { useState, useEffect } from 'react';
import GridLayout from 'react-grid-layout';
import { WidgetRenderer } from '../widgets/WidgetRenderer';
import { apiClient } from '@/lib/api-client';
import { SkeletonCard } from '../common/Skeleton';
import 'react-grid-layout/css/styles.css';
import 'react-resizable/css/styles.css';

interface WidgetConfig {
  id: string;
  widget_key: string;
  config?: Record<string, any>;
}

interface LayoutItem {
  i: string;
  x: number;
  y: number;
  w: number;
  h: number;
  minW?: number;
  minH?: number;
}

interface Dashboard {
  id: string;
  layout: LayoutItem[];
  widgets: WidgetConfig[];
}

export function DashboardGrid() {
  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchDashboard();
  }, []);

  async function fetchDashboard() {
    try {
      setLoading(true);
      const response = await apiClient.getDefaultDashboard();
      
      if (response.success) {
        setDashboard(response.data);
      }
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  async function handleLayoutChange(newLayout: LayoutItem[]) {
    if (!dashboard) return;

    // Update dashboard layout
    try {
      await apiClient.updateDashboard(dashboard.id, {
        layout: newLayout,
      });

      setDashboard({
        ...dashboard,
        layout: newLayout,
      });
    } catch (err: any) {
      console.error('Failed to update dashboard layout:', err);
    }
  }

  if (loading) {
    return (
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <SkeletonCard />
        <SkeletonCard />
        <SkeletonCard />
        <SkeletonCard />
      </div>
    );
  }

  if (error || !dashboard) {
    return (
      <div className="text-center py-12">
        <p className="text-error">Failed to load dashboard</p>
        <button
          onClick={fetchDashboard}
          className="mt-4 btn btn-primary"
        >
          Retry
        </button>
      </div>
    );
  }

  return (
    <GridLayout
      className="layout"
      layout={dashboard.layout}
      cols={12}
      rowHeight={100}
      width={1200}
      onLayoutChange={handleLayoutChange}
      draggableHandle=".widget-drag-handle"
      isDraggable
      isResizable
      compactType="vertical"
      preventCollision={false}
    >
      {dashboard.widgets.map(widget => (
        <div key={widget.id} className="bg-white rounded-lg shadow-sm overflow-hidden">
          <div className="widget-drag-handle h-2 bg-gray-100 hover:bg-primary-100 cursor-move transition-colors" />
          <div className="h-[calc(100%-8px)]">
            <WidgetRenderer widget={widget} />
          </div>
        </div>
      ))}
    </GridLayout>
  );
}

