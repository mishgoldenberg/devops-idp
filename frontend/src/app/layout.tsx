import type { Metadata } from 'next';
import { Inter } from 'next/font/google';
import './globals.css';
import { Providers } from '@/components/providers/Providers';
import { Header } from '@/components/layout/Header';
import { ChatBot } from '@/components/chat/ChatBot';

const inter = Inter({ subsets: ['latin'] });

export const metadata: Metadata = {
  title: 'DevOps Control Center',
  description: 'Production-grade internal DevOps platform',
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className={inter.className}>
        <Providers>
          <Header />
          <main className="min-h-screen bg-gray-50 dark:bg-gray-900">
            {children}
          </main>
          <ChatBot />
        </Providers>
      </body>
    </html>
  );
}

