import { NextResponse } from "next/server";

// Process-only probe; gateway and Redis readiness are surfaced in the dashboard.
export function GET() {
  return NextResponse.json({ status: "ok" }, { headers: { "Cache-Control": "no-store" } });
}
