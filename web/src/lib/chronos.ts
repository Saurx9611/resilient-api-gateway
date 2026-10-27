export type JobState = "queued" | "running" | "succeeded" | "dead" | "canceled";
export type JobType = "image_metadata" | "csv_transform" | "synthetic_report" | "notification_simulation";
export const JOB_STATES: JobState[] = ["queued", "running", "succeeded", "dead", "canceled"];
export const JOB_TYPES: JobType[] = ["image_metadata", "csv_transform", "synthetic_report", "notification_simulation"];

export interface Job {
  id: number;
  job_type: JobType;
  payload?: Record<string, unknown>;
  state: JobState;
  attempts: number;
  max_attempts: number;
  max_runtime_seconds?: number;
  available_at: string;
  priority: number;
  worker_id: string | null;
  lease_until: string | null;
  generation?: number;
  replay_of?: number | null;
  result?: Record<string, unknown> | null;
  last_error_code?: string | null;
  cancel_requested_at?: string | null;
  submitted_by?: string | null;
  created_at: string;
  updated_at?: string;
}

export interface Page<T> { items: T[]; next_cursor: string | null }
export interface QueueStats {
  states: Record<JobState, number>;
  ready: number;
  delayed: number;
  running: number;
  queued: number;
  oldest_queued_at: string | null;
}
export interface Worker {
  worker_id: string;
  name: string;
  pid: number;
  hostname: string;
  concurrency: number;
  state: "active" | "draining" | "stopped";
  started_at: string;
  heartbeat_at: string;
  stale: boolean;
  running_jobs?: number;
}
export interface JobEvent {
  id: number;
  job_id: number;
  job_type?: JobType;
  event_type: string;
  attempt: number;
  worker_id: string | null;
  generation: number;
  occurred_at: string;
  details: Record<string, unknown>;
}
export interface Schedule {
  schedule_id: string;
  job_type: JobType;
  cron_expression: string;
  timezone: string;
  priority: number;
  enabled: boolean;
  next_occurrence_at: string;
  created_by: string;
  created_at: string;
}
export interface Observability {
  hours: number;
  bucket_size: "hour" | "day";
  buckets: { bucket: string; succeeded: number; failed: number; retried: number; submitted: number }[];
  latency: { samples: number; p50_ms: number | null; p95_ms: number | null; p99_ms: number | null; unit: "ms"; definition: string };
  totals: { succeeded: number; failed: number; retried: number; failure_rate_percent: number | null };
  operations: {
    pending: number; running: number; completed: number; failed: number; dead_letter: number;
    active_workers: number; idle_workers: number; jobs_completed_per_second: number;
    retries_24h: number; recovered_24h: number; expired_leases_24h: number; retry_rate_24h: number | null;
    queue_p50_ms: number | null; queue_p95_ms: number | null; queue_p99_ms: number | null;
    execution_p50_ms: number | null; execution_p95_ms: number | null; execution_p99_ms: number | null;
    samples: number; window_hours: number;
    pool: { pool_size: number; pool_max_size: number; pool_available: number; requests_waiting: number; requests_queued: number; requests_wait_ms: number };
  };
}
export interface QueuePolicy {
  default_max_attempts: number;
  default_max_runtime_seconds: number;
  updated_by: string;
  updated_at: string;
}

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) { super(message); }
}

export async function apiRequest<T>(token: string, path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const response = await fetch(`/api/chronos/${path.replace(/^\//, "")}`, { ...init, headers, cache: "no-store" });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = data?.error ?? {};
    throw new ApiError(response.status, error.code ?? "request_failed", error.message ?? "The request could not be completed.");
  }
  return data as T;
}

export function queryString(values: Record<string, string | number | undefined | null>): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) if (value !== undefined && value !== null && value !== "") query.set(key, String(value));
  const result = query.toString();
  return result ? `?${result}` : "";
}

export function shortId(id: string | number, length = 10): string { return String(id).length > length ? `${String(id).slice(0, length)}…` : String(id); }
export function formatDate(value?: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "—" : new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
}
export function timeAgo(value?: string | null): string {
  if (!value) return "—";
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(value).getTime()) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

export const jobTypeLabels: Record<JobType, string> = {
  image_metadata: "Image metadata", csv_transform: "CSV transform",
  synthetic_report: "Synthetic report", notification_simulation: "Notification simulation",
};
