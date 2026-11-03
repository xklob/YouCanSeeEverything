import type { MapConfig } from "./types";
export type MapProvider = "openstreetmap" | "google";

// An explicit choice lasts only for this page's lifetime, across view remounts.
// No API key or preference is persisted in browser storage.
let explicitChoice: MapProvider | null = null;
export function rememberMapProvider(provider: MapProvider | null): void {
  explicitChoice = provider;
}
export function preferredMapProvider(config: MapConfig): MapProvider {
  if (!config.google_maps_configured || !config.google_maps_api_key)
    return "openstreetmap";
  return (
    explicitChoice ||
    (config.provider === "google" ? "google" : "openstreetmap")
  );
}
