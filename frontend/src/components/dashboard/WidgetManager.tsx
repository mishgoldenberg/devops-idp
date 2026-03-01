'use client';

import { useState } from 'react';
import { Card } from '@/components/common/Card';
import { Badge } from '@/components/common/Badge';
import { X, Plus, Grid, CheckCircle } from 'lucide-react';
import { WIDGET_LIBRARY, WidgetDefinition } from '@/lib/widget-library';

interface WidgetManagerProps {
  activeWidgets: string[];
  onAddWidget: (widgetId: string) => void;
  onRemoveWidget: (widgetId: string) => void;
  onClose: () => void;
}

const categoryColors = {
  devops: 'bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300',
  quality: 'bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-300',
  integration: 'bg-purple-100 dark:bg-purple-900/30 text-purple-700 dark:text-purple-300',
  monitoring: 'bg-orange-100 dark:bg-orange-900/30 text-orange-700 dark:text-orange-300',
};

export function WidgetManager({ activeWidgets, onAddWidget, onRemoveWidget, onClose }: WidgetManagerProps) {
  const [selectedCategory, setSelectedCategory] = useState<string>('all');
  
  const categories = [
    { id: 'all', name: 'All Widgets' },
    { id: 'devops', name: 'DevOps' },
    { id: 'quality', name: 'Quality' },
    { id: 'integration', name: 'Integration' },
    { id: 'monitoring', name: 'Monitoring' },
  ];

  const widgets = Object.values(WIDGET_LIBRARY).filter(
    (widget) => selectedCategory === 'all' || widget.category === selectedCategory
  );

  const isWidgetActive = (widgetId: string) => activeWidgets.includes(widgetId);

  return (
    <div className="fixed inset-0 bg-black/50 dark:bg-black/70 backdrop-blur-sm z-50 flex items-center justify-center p-4">
      <Card className="max-w-4xl w-full max-h-[80vh] overflow-hidden flex flex-col">
        {/* Header */}
        <div className="p-6 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 bg-blue-600 dark:bg-blue-700 rounded-lg flex items-center justify-center">
              <Grid className="w-5 h-5 text-white" />
            </div>
            <div>
              <h2 className="text-xl font-bold text-gray-900 dark:text-gray-100">Customize Dashboard</h2>
              <p className="text-sm text-gray-600 dark:text-gray-400">Add or remove widgets to personalize your view</p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="p-2 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors"
          >
            <X className="w-5 h-5 text-gray-500 dark:text-gray-400" />
          </button>
        </div>

        {/* Category Tabs */}
        <div className="px-6 pt-4 border-b border-gray-200 dark:border-gray-700">
          <div className="flex gap-2 overflow-x-auto pb-2">
            {categories.map((category) => (
              <button
                key={category.id}
                onClick={() => setSelectedCategory(category.id)}
                className={`px-4 py-2 rounded-lg text-sm font-medium whitespace-nowrap transition-colors ${
                  selectedCategory === category.id
                    ? 'bg-blue-600 dark:bg-blue-700 text-white'
                    : 'bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600'
                }`}
              >
                {category.name}
              </button>
            ))}
          </div>
        </div>

        {/* Widget Grid */}
        <div className="flex-1 overflow-y-auto p-6">
          <div className="grid md:grid-cols-2 gap-4">
            {widgets.map((widget) => {
              const isActive = isWidgetActive(widget.id);
              return (
                <Card
                  key={widget.id}
                  className={`p-4 transition-all ${
                    isActive
                      ? 'border-2 border-blue-500 dark:border-blue-600 bg-blue-50 dark:bg-blue-900/30'
                      : 'border border-gray-200 dark:border-gray-700 hover:border-blue-300 dark:hover:border-blue-600 hover:shadow-md'
                  }`}
                >
                  <div className="flex items-start justify-between mb-2">
                    <div className="flex-1">
                      <div className="flex items-center gap-2 mb-1">
                        <h3 className="font-semibold text-gray-900 dark:text-gray-100">{widget.name}</h3>
                        {isActive && (
                          <CheckCircle className="w-4 h-4 text-blue-600 dark:text-blue-400" />
                        )}
                      </div>
                      <p className="text-sm text-gray-600 dark:text-gray-400 mb-3">{widget.description}</p>
                      <Badge className={categoryColors[widget.category]}>
                        {widget.category}
                      </Badge>
                    </div>
                  </div>
                  <div className="flex gap-2 mt-4">
                    {isActive ? (
                      <button
                        onClick={() => onRemoveWidget(widget.id)}
                        className="flex-1 px-4 py-2 bg-red-600 dark:bg-red-700 text-white rounded-lg text-sm font-medium hover:bg-red-700 dark:hover:bg-red-600 transition-colors"
                      >
                        <X className="w-4 h-4 inline mr-1" />
                        Remove
                      </button>
                    ) : (
                      <button
                        onClick={() => onAddWidget(widget.id)}
                        className="flex-1 px-4 py-2 bg-blue-600 dark:bg-blue-700 text-white rounded-lg text-sm font-medium hover:bg-blue-700 dark:hover:bg-blue-600 transition-colors"
                      >
                        <Plus className="w-4 h-4 inline mr-1" />
                        Add to Dashboard
                      </button>
                    )}
                  </div>
                </Card>
              );
            })}
          </div>
        </div>

        {/* Footer */}
        <div className="p-6 border-t border-gray-200 dark:border-gray-700 flex items-center justify-between bg-gray-50 dark:bg-gray-800/50">
          <p className="text-sm text-gray-600 dark:text-gray-400">
            {activeWidgets.length} widgets active
          </p>
          <button
            onClick={onClose}
            className="px-6 py-2 bg-blue-600 dark:bg-blue-700 text-white rounded-lg font-medium hover:bg-blue-700 dark:hover:bg-blue-600 transition-colors"
          >
            Done
          </button>
        </div>
      </Card>
    </div>
  );
}

