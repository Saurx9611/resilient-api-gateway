export async function GET() {
  const origin = process.env.CHRONOS_API_URL ?? "http://127.0.0.1:8000";
  try {
    const response = await fetch(`${origin.replace(/\/$/, "")}/openapi.json`, { cache: "no-store", signal: AbortSignal.timeout(5000) });
    return new Response(response.body, { status: response.status, headers: { "Content-Type": "application/json" } });
  } catch {
    return Response.json({ error: { code: "control_plane_unavailable", message: "API documentation is temporarily unavailable." } }, { status: 502 });
  }
}
