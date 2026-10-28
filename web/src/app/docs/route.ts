import { chronosApiOrigin } from "@/lib/server-api";

export async function GET() {
  const origin = chronosApiOrigin();
  try {
    const response = await fetch(`${origin.replace(/\/$/, "")}/docs`, { cache: "no-store", signal: AbortSignal.timeout(5000) });
    return new Response(response.body, { status: response.status, headers: { "Content-Type": response.headers.get("content-type") ?? "text/html; charset=utf-8" } });
  } catch {
    return new Response("Control-plane API documentation is unavailable.", { status: 502, headers: { "Content-Type": "text/plain; charset=utf-8" } });
  }
}
