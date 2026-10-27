"use client";

import { useSyncExternalStore } from "react";

function subscribe(callback: () => void) {
  window.addEventListener("chronos:preferences", callback);
  window.addEventListener("storage", callback);
  return () => { window.removeEventListener("chronos:preferences", callback); window.removeEventListener("storage", callback); };
}
function getSnapshot() {
  const value = Number(localStorage.getItem("chronos.refresh-ms"));
  return [15000, 30000, 60000].includes(value) ? value : 15000;
}
export function useRefreshPreference() {
  return useSyncExternalStore(subscribe, getSnapshot, () => 15000);
}
