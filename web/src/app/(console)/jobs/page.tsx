import { JobsPage } from "@/components/pages/jobs-page";

export default async function Page({ searchParams }: { searchParams: Promise<{ state?: string }> }) {
  const params = await searchParams;
  return <JobsPage initialState={params.state ?? ""} />;
}
