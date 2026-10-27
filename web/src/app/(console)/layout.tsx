import { AuthenticatedLayout } from "@/components/app-shell";

export default function ConsoleLayout({ children }: { children: React.ReactNode }) {
  return <AuthenticatedLayout>{children}</AuthenticatedLayout>;
}
