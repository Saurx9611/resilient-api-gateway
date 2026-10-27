"use client";

import { createContext, useCallback, useContext, useMemo, useSyncExternalStore } from "react";

interface AuthContextValue { token: string | null; ready: boolean; connect: (token: string) => void; disconnect: () => void }
const AuthContext = createContext<AuthContextValue | null>(null);

function subscribeSession(callback: () => void) {
  window.addEventListener("chronos:auth", callback);
  window.addEventListener("storage", callback);
  return () => { window.removeEventListener("chronos:auth", callback); window.removeEventListener("storage", callback); };
}
function getSessionToken() { return sessionStorage.getItem("chronos.api-token"); }
function subscribeHydration() { return () => undefined; }

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const token = useSyncExternalStore(subscribeSession, getSessionToken, () => null);
  const ready = useSyncExternalStore(subscribeHydration, () => true, () => false);
  const connect = useCallback((value: string) => { sessionStorage.setItem("chronos.api-token", value); window.dispatchEvent(new Event("chronos:auth")); }, []);
  const disconnect = useCallback(() => { sessionStorage.removeItem("chronos.api-token"); window.dispatchEvent(new Event("chronos:auth")); }, []);
  const value = useMemo(() => ({ token, ready, connect, disconnect }), [token, ready, connect, disconnect]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside AuthProvider");
  return value;
}
