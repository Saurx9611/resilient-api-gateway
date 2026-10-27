import { JobsPage } from "@/components/pages/jobs-page";

export default function Page() {
  return <JobsPage initialState="dead" title="Dead letter queue" description="Terminal jobs that exhausted their configured attempts or failed permanently. Review event history before retrying or replaying." />;
}
