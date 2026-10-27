"use client";

import { useCallback, useEffect, useState } from "react";
import { ApiError, apiRequest } from "@/lib/chronos";
import { useAuth } from "@/components/auth-provider";
import { useRefreshPreference } from "@/hooks/use-refresh-preference";

export function usePoll<T>(path: string, interval = 5000) {
  const { token } = useAuth();
  const [snapshot, setSnapshot] = useState<{ token: string; path: string; data: T | null; error: Error | null; updatedAt: Date; loading: boolean } | null>(null);
  const [revision, setRevision] = useState(0);
  const preferredInterval = useRefreshPreference();
  const refresh = useCallback(() => setRevision((value) => value + 1), []);
  const effectiveInterval = Math.max(interval, preferredInterval);
  useEffect(() => {
    if (!token) return;
    let active = true;
    const controller = new AbortController();
    let timer = 0;
    const run = async () => {
      try {
        const result = await apiRequest<T>(token, path, { signal: controller.signal });
        if (active) setSnapshot({ token, path, data: result, error: null, updatedAt: new Date(), loading: false });
      } catch (value) {
        if (!active || (value instanceof Error && value.name === "AbortError")) return;
        const error = value instanceof ApiError ? value : value instanceof Error ? value : new Error("Could not load data");
        if (active) setSnapshot((current) => current?.token === token && current.path === path
          ? { ...current, error, loading: false }
          : { token, path, data: null, error, updatedAt: new Date(), loading: false });
      } finally {
        if (active) {
          timer = window.setTimeout(() => { void run(); }, effectiveInterval);
        }
      }
    };
    void run();
    return () => { active = false; controller.abort(); window.clearTimeout(timer); };
  }, [token, path, effectiveInterval, revision]);
  const current = token && snapshot?.token === token && snapshot.path === path ? snapshot : null;
  return { data: current?.data ?? null, loading: token ? (current?.loading ?? true) : false,
    error: current?.error ?? null, updatedAt: current?.updatedAt ?? null, refresh };
}
