import { gatewayRequest } from "../_gateway";

export const dynamic = "force-dynamic";
export async function GET() { return gatewayRequest("status", "GET"); }
