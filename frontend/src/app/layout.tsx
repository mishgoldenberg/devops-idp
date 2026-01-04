import type { Metadata } from 'next';
import { Inter } from 'next/font/google';
import './globals.css';
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
    <html lang="en">
      <body className={inter.className}>
        <Header />
        <main className="min-h-screen">
          {children}
        </main>
        <ChatBot />
      </body>
    </html>
  );
}

