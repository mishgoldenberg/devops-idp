'use client';

import { useState } from 'react';
import { Card } from '@/components/common/Card';
import { Badge } from '@/components/common/Badge';
import { Search, BookOpen, Settings, Database, Shield, Wrench, Code, Star, Eye, Clock } from 'lucide-react';

const categories = [
  { id: 'all', name: 'All Categories', icon: BookOpen },
  { id: 'getting-started', name: 'Getting Started', icon: Star },
  { id: 'setup', name: 'Setup & Config', icon: Settings },
  { id: 'database', name: 'Database', icon: Database },
  { id: 'security', name: 'Security', icon: Shield },
  { id: 'deployment', name: 'Deployment', icon: Wrench },
  { id: 'debugging', name: 'Debugging', icon: Code },
];

const tags = ['React', 'Node.js', 'Python', 'Docker', 'AWS', 'API'];

const featuredGuides = [
  {
    id: 1,
    title: 'Complete React App Setup with TypeScript',
    description: 'Learn how to set up a modern React application with TypeScript, ESLint, and Prettier from scratch.',
    category: 'getting-started',
    readTime: '15 min read',
    difficulty: 'Beginner',
    author: 'John Doe',
    views: '2.3k views',
    featured: true,
    gradient: 'from-blue-500 to-purple-600',
  },
  {
    id: 2,
    title: 'Docker Deployment Best Practices',
    description: 'Master containerization with Docker and deploy your applications efficiently to production.',
    category: 'deployment',
    readTime: '12 min read',
    difficulty: 'Intermediate',
    author: 'Jane Smith',
    views: '1.8k views',
    featured: true,
    gradient: 'from-green-500 to-teal-600',
  },
];

const latestGuides = [
  {
    id: 3,
    title: 'How to Set Up Your First API Endpoint',
    description: 'Step-by-step guide to creating REST APIs with Express.js and handle HTTP requests efficiently.',
    category: 'getting-started',
    readTime: '10 min read',
    difficulty: 'Getting Started',
    author: 'John Doe',
    likes: 145,
    comments: 23,
    color: 'blue',
  },
  {
    id: 4,
    title: 'MongoDB Connection and Schema Design',
    description: 'Best practices for connecting to MongoDB and designing efficient schemas for your application.',
    category: 'database',
    readTime: '12 min read',
    difficulty: 'Database',
    author: 'Sarah Wilson',
    likes: 89,
    comments: 15,
    color: 'green',
  },
  {
    id: 5,
    title: 'JWT Authentication Implementation',
    description: 'Implement secure authentication in your Node.js applications with citizen tokens.',
    category: 'security',
    readTime: '18 min read',
    difficulty: 'Security',
    author: 'Mike Johnson',
    likes: 234,
    comments: 42,
    color: 'purple',
  },
  {
    id: 6,
    title: 'AWS EC2 Deployment Guide',
    description: 'Deploy your web application to AWS EC2 with nginx, SSL certificates, and CI/CD setup.',
    category: 'deployment',
    readTime: '22 min read',
    difficulty: 'Deployment',
    author: 'Emma Davis',
    likes: 178,
    comments: 31,
    color: 'orange',
  },
  {
    id: 7,
    title: 'Chrome DevTools for Performance',
    description: 'Master Chrome DevTools to debug performance issues and optimize your web applications.',
    category: 'debugging',
    readTime: '14 min read',
    difficulty: 'Database',
    author: 'Alex Chen',
    likes: 156,
    comments: 28,
    color: 'red',
  },
  {
    id: 8,
    title: 'ESLint and Prettier Configuration',
    description: 'Set up code formatting and linting tools to maintain consistent code quality across your team.',
    category: 'setup',
    readTime: '8 min read',
    difficulty: 'Setup & Config',
    author: 'David Kim',
    likes: 92,
    comments: 17,
    color: 'indigo',
  },
];

const difficultyColors: Record<string, string> = {
  'Beginner': 'bg-green-100 text-green-700',
  'Intermediate': 'bg-yellow-100 text-yellow-700',
  'Advanced': 'bg-red-100 text-red-700',
};

const categoryColors: Record<string, string> = {
  'blue': 'bg-blue-50 border-blue-200',
  'green': 'bg-green-50 border-green-200',
  'purple': 'bg-purple-50 border-purple-200',
  'orange': 'bg-orange-50 border-orange-200',
  'red': 'bg-red-50 border-red-200',
  'indigo': 'bg-indigo-50 border-indigo-200',
};

export default function HowToPage() {
  const [selectedCategory, setSelectedCategory] = useState('all');
  const [searchQuery, setSearchQuery] = useState('');

  return (
    <div className="min-h-screen bg-gray-50 dark:bg-gray-900">
      {/* Header */}
      <div className="bg-white dark:bg-gray-900 border-b border-gray-200 dark:border-gray-800">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
          <div className="flex items-center justify-between mb-6">
            <div>
              <h1 className="text-3xl font-bold text-gray-900 dark:text-gray-100">Developer How-To Guides</h1>
              <p className="mt-2 text-gray-600 dark:text-gray-300">Step-by-step tutorials and guides to help you build amazing applications</p>
            </div>
            <div className="relative">
              <input
                type="text"
                placeholder="Search guides..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="w-80 pl-10 pr-4 py-2 border border-gray-300 dark:border-gray-700 rounded-lg bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
              />
              <Search className="absolute left-3 top-2.5 w-5 h-5 text-gray-400 dark:text-gray-500" />
            </div>
          </div>

          {/* Category Filters */}
          <div className="flex items-center gap-2 overflow-x-auto pb-2">
            {categories.map((cat) => (
              <button
                key={cat.id}
                onClick={() => setSelectedCategory(cat.id)}
                className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium whitespace-nowrap transition-colors ${
                  selectedCategory === cat.id
                    ? 'bg-blue-600 text-white'
                    : 'bg-gray-100 dark:bg-gray-800 text-gray-700 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-700'
                }`}
              >
                <cat.icon className="w-4 h-4" />
                {cat.name}
              </button>
            ))}
          </div>

          {/* Sort Options */}
          <div className="flex items-center gap-4 mt-4">
            <span className="text-sm text-gray-600 dark:text-gray-300">Sort by:</span>
            <select className="px-3 py-1.5 border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-800 text-sm text-gray-900 dark:text-gray-100 focus:ring-2 focus:ring-blue-500">
              <option>Recent</option>
              <option>Popular</option>
              <option>Most Viewed</option>
            </select>
            <button className="px-4 py-1.5 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700">
              + New Guide
            </button>
          </div>
        </div>
      </div>

      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        {/* Featured Guides */}
        <div className="mb-12">
          <h2 className="text-2xl font-bold text-gray-900 dark:text-gray-100 mb-6">Featured Guides</h2>
          <div className="grid md:grid-cols-2 gap-6">
            {featuredGuides.map((guide) => (
              <Card key={guide.id} className="overflow-hidden hover:shadow-lg transition-shadow cursor-pointer">
                <div className={`h-40 bg-gradient-to-r ${guide.gradient} p-6 text-white`}>
                  <Badge variant="secondary" className="bg-white/20 text-white mb-3">
                    <Star className="w-3 h-3 mr-1" />
                    Featured Guide
                  </Badge>
                  <h3 className="text-xl font-bold mb-2">{guide.title}</h3>
                  <p className="text-white/90 text-sm">{guide.description}</p>
                </div>
                <div className="p-6">
                  <div className="flex items-center justify-between text-sm text-gray-600">
                    <div className="flex items-center gap-4">
                      <span className="flex items-center gap-1">
                        <Clock className="w-4 h-4" />
                        {guide.readTime}
                      </span>
                      <Badge className={difficultyColors[guide.difficulty]}>{guide.difficulty}</Badge>
                    </div>
                    <span className="flex items-center gap-1">
                      <Eye className="w-4 h-4" />
                      {guide.views}
                    </span>
                  </div>
                  <button className="mt-4 w-full px-4 py-2 bg-blue-600 text-white rounded-lg font-medium hover:bg-blue-700">
                    Read Guide
                  </button>
                </div>
              </Card>
            ))}
          </div>
        </div>

        {/* Latest Guides */}
        <div>
          <div className="flex items-center justify-between mb-6">
            <h2 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Latest Guides</h2>
            <a href="#" className="text-blue-600 dark:text-blue-400 hover:text-blue-700 dark:hover:text-blue-300 font-medium text-sm">View all guides →</a>
          </div>

          <div className="grid md:grid-cols-2 lg:grid-cols-3 gap-6">
            {latestGuides.map((guide) => (
              <Card 
                key={guide.id} 
                className={`border-l-4 ${categoryColors[guide.color]} hover:shadow-lg transition-shadow cursor-pointer`}
              >
                <div className="p-6">
                  <Badge className="mb-3">{guide.difficulty}</Badge>
                  <h3 className="text-lg font-bold text-gray-900 mb-2">{guide.title}</h3>
                  <p className="text-sm text-gray-600 dark:text-gray-300 mb-4 line-clamp-2">{guide.description}</p>
                  
                  <div className="flex items-center justify-between text-sm text-gray-500 mb-4">
                    <span className="flex items-center gap-1">
                      <Clock className="w-4 h-4" />
                      {guide.readTime}
                    </span>
                  </div>

                  <div className="flex items-center justify-between pt-4 border-t border-gray-200 dark:border-gray-700">
                    <span className="text-xs text-gray-600 dark:text-gray-400">{guide.author}</span>
                    <div className="flex items-center gap-3 text-xs text-gray-600 dark:text-gray-400">
                      <span>❤️ {guide.likes}</span>
                      <span>💬 {guide.comments}</span>
                    </div>
                  </div>
                </div>
              </Card>
            ))}
          </div>
        </div>

        {/* Newsletter CTA */}
        <div className="mt-12 bg-gradient-to-r from-blue-600 to-purple-600 rounded-2xl p-8 text-center text-white">
          <h2 className="text-2xl font-bold mb-2">Stay Updated with New Guides</h2>
          <p className="text-blue-100 mb-6">Get the latest how-to guides and tutorials delivered to your inbox</p>
          <div className="flex gap-3 max-w-md mx-auto">
            <input
              type="email"
              placeholder="Enter your email"
              className="flex-1 px-4 py-3 rounded-lg text-gray-900 focus:outline-none focus:ring-2 focus:ring-white"
            />
            <button className="px-6 py-3 bg-white text-blue-600 rounded-lg font-medium hover:bg-blue-50">
              Subscribe
            </button>
          </div>
        </div>

        {/* Popular Tags */}
        <div className="mt-8">
          <h3 className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-3">Popular Tags:</h3>
          <div className="flex flex-wrap gap-2">
            {tags.map((tag) => (
              <button
                key={tag}
                className="px-3 py-1 bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-700 rounded-full text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700"
              >
                #{tag}
              </button>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

