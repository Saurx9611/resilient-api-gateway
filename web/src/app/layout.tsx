import type { Metadata } from "next";
import { ThemeProvider } from "@/components/theme-provider";
import { Toaster } from "sonner";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "Chronos · Control plane", template: "%s · Chronos" },
  description: "Observe and operate the Chronos distributed job control plane.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en" suppressHydrationWarning className="h-full">
    <body className="min-h-full antialiased">
      <ThemeProvider attribute="class" defaultTheme="dark" enableSystem disableTransitionOnChange>
        {children}
        <Toaster theme="system" richColors position="bottom-right" closeButton />
      </ThemeProvider>
    </body>
  </html>;
}
