/** Server-only internal Chronos API origin used by BFF routes. */
export function chronosApiOrigin(): string {
  const configuredUrl = process.env.CHRONOS_API_URL;
  if (configuredUrl) return configuredUrl.replace(/\/$/, "");

  const hostPort = process.env.CHRONOS_API_INTERNAL_HOSTPORT;
  if (!hostPort) return "http://127.0.0.1:8000";
  return `${/^https?:\/\//i.test(hostPort) ? "" : "http://"}${hostPort}`.replace(/\/$/, "");
}
