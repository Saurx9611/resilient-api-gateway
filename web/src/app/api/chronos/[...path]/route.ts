import { NextRequest } from "next/server";
import { chronosApiOrigin } from "@/lib/server-api";

const maxBody = 64 * 1024;
const resourcePattern = {
  job: /^v1\/jobs\/[1-9][0-9]{0,18}$/,
  jobAction: /^v1\/jobs\/[1-9][0-9]{0,18}\/(attempts|cancel|retry|replay)$/,
  schedule: /^v1\/schedules\/[0-9a-f-]{36}$/i,
  scheduleCancel: /^v1\/schedules\/[0-9a-f-]{36}\/cancel$/i,
};

function allowed(path: string, method: string): boolean {
  if (path === "v1/jobs") return ["GET", "POST"].includes(method);
  if (path === "v1/jobs" && method === "GET") return true;
  if (resourcePattern.job.test(path)) return method === "GET";
  if (resourcePattern.jobAction.test(path)) return path.endsWith("/attempts") ? method === "GET" : method === "POST";
  if (path === "v1/schedules") return ["GET", "POST"].includes(method);
  if (resourcePattern.schedule.test(path)) return method === "GET";
  if (resourcePattern.scheduleCancel.test(path)) return method === "POST";
  if (["v1/workers", "v1/queue/stats", "v1/events", "v1/observability"].includes(path)) return method === "GET";
  if (path === "v1/settings/queue") return ["GET", "PUT"].includes(method);
  return false;
}

async function readBoundedBody(request: NextRequest): Promise<Uint8Array> {
  const length = Number(request.headers.get("content-length") ?? 0);
  if (length > maxBody) throw new RangeError("request_too_large");
  if (!request.body) return new Uint8Array();
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > maxBody) {
        await reader.cancel();
        throw new RangeError("request_too_large");
      }
      chunks.push(value);
    }
  } finally { reader.releaseLock(); }
  const body = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { body.set(chunk, offset); offset += chunk.byteLength; }
  return body;
}

async function proxy(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const { path: parts } = await context.params;
  const path = parts.join("/");
  if (!allowed(path, request.method)) return Response.json({ error: { code: "not_found", message: "The requested control-plane endpoint is not available." } }, { status: 404 });
  const authorization = request.headers.get("authorization");
  if (!authorization?.startsWith("Bearer ") || authorization.length > 300) return Response.json({ error: { code: "unauthorized", message: "Connect a Chronos bearer token to continue." } }, { status: 401 });
  let body: string | undefined;
  if (!["GET", "HEAD"].includes(request.method)) {
    if (!request.headers.get("content-type")?.toLowerCase().startsWith("application/json")) return Response.json({ error: { code: "application_json_required", message: "Send JSON content." } }, { status: 415 });
    try { body = new TextDecoder().decode(await readBoundedBody(request)); }
    catch { return Response.json({ error: { code: "request_too_large", message: "Request exceeds the 64 KiB limit." } }, { status: 413 }); }
  }
  const origin = chronosApiOrigin();
  const headers = new Headers({ Authorization: authorization, Accept: "application/json" });
  const idempotency = request.headers.get("idempotency-key");
  if (idempotency) headers.set("Idempotency-Key", idempotency);
  if (body) headers.set("Content-Type", "application/json");
  try {
    const upstream = await fetch(`${origin.replace(/\/$/, "")}/${path}${request.nextUrl.search}`, {
      method: request.method, headers, body, cache: "no-store", signal: AbortSignal.timeout(8_000),
    });
    const responseHeaders = new Headers({ "Content-Type": upstream.headers.get("content-type") ?? "application/json" });
    for (const name of ["x-request-id", "ratelimit-limit", "ratelimit-remaining", "retry-after", "www-authenticate"]) {
      const value = upstream.headers.get(name); if (value) responseHeaders.set(name, value);
    }
    return new Response(upstream.body, { status: upstream.status, headers: responseHeaders });
  } catch (error) {
    const timeout = error instanceof Error && error.name === "TimeoutError";
    return Response.json({ error: { code: timeout ? "upstream_timeout" : "control_plane_unavailable", message: timeout ? "The control plane did not respond within eight seconds." : "The control plane is temporarily unavailable." } }, { status: timeout ? 504 : 502 });
  }
}

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
