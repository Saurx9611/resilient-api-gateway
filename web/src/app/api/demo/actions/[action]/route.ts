import { NextRequest, NextResponse } from "next/server";
import { gatewayRequest } from "../../_gateway";

export const dynamic = "force-dynamic";
export async function POST(request: NextRequest, context: { params: Promise<{ action: string }> }) {
  const origin = request.headers.get("origin");
  const host = request.headers.get("host");
  const forwardedProto = request.headers.get("x-forwarded-proto")?.split(",", 1)[0]?.trim();
  const protocol = forwardedProto ? `${forwardedProto}:` : new URL(request.url).protocol;
  let source: URL | undefined;
  try { if (origin) source = new URL(origin); } catch { /* malformed Origin is rejected below */ }
  if (!source || !host || source.host !== host || source.protocol !== protocol || source.username || source.password || source.pathname !== "/" || source.search || source.hash) {
    return NextResponse.json({ error: "Same-origin request required" }, { status: 403 });
  }
  const contentLength = request.headers.get("content-length");
  if (contentLength !== null && contentLength !== "0") return NextResponse.json({ error: "Request body not supported" }, { status: 413 });
  if (request.body) {
    const reader = request.body.getReader();
    const first = await reader.read();
    if (!first.done) {
      await reader.cancel();
      return NextResponse.json({ error: "Request body not supported" }, { status: 413 });
    }
  }
  const { action } = await context.params;
  return gatewayRequest(`actions/${action}`, "POST");
}
