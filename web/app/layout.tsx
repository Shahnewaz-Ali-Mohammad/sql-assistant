import type { Metadata } from 'next';
import './styles.css';

export const metadata: Metadata = {
  title: 'SQL Assistant',
  description: 'Generate read-only SQL from plain language and your connected database schema.',
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}</body></html>;
}
