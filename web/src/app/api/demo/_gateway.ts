import { NextResponse } from "next/server";

const actions = new Set(["normal", "rate-burst", "circuit-recovery"]);

function configuration() {
  const hostport = process.env.GATEWAY_INTERNAL_HOSTPORT;
  const raw = process.env.GATEWAY_INTERNAL_URL ?? (hostport ? `http://${hostport}` : "http://127.0.0.1:8080");
  let base: URL;
  try {
    base = new URL(raw);
  } catch {
    throw new Error("Gateway endpoint is not configured");
  }
  if (!["http:", "https:"].includes(base.protocol) || base.username || base.password || base.pathname !== "/" || base.search || base.hash) {
    throw new Error("Gateway endpoint is not configured");
  }
  const token = process.env.GATEWAY_DEMO_TOKEN;
  if (!token || token.length < 32 || token.length > 256) throw new Error("Gateway demo is not configured");
  return { base, token };
}

export async function gatewayRequest(path: "status" | `actions/${string}`, method: "GET" | "POST") {
  if (path.startsWith("actions/") && !actions.has(path.slice("actions/".length))) {
    return NextResponse.json({ error: "Unknown action" }, { status: 404 });
  }
  try {
    const { base, token } = configuration();
    const response = await fetch(new URL(`/demo/${path}`, base), {
      method,
      headers: { Authorization: `Bearer ${token}` },
      cache: "no-store",
      signal: AbortSignal.timeout(12_000),
    });
    const reader = response.body?.getReader();
    const decoder = new TextDecoder();
    let raw = "";
    let bytes = 0;
    if (reader) {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        bytes += value.byteLength;
        if (bytes > 16_384) {
          await reader.cancel();
          return NextResponse.json({ error: "Gateway response too large" }, { status: 502 });
        }
        raw += decoder.decode(value, { stream: true });
      }
      raw += decoder.decode();
    }
    let data: unknown;
    try { data = JSON.parse(raw); } catch { data = { error: "Gateway returned an invalid response" }; }
    return NextResponse.json(data, { status: response.status, headers: { "Cache-Control": "no-store" } });
  } catch {
    return NextResponse.json({ error: "Gateway is unavailable or demo access is not configured" }, { status: 503, headers: { "Cache-Control": "no-store" } });
  }
}
