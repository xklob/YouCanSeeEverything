export interface LifeEvent {
  id: string;
  title: string;
  body: string;
  occurred_at: string | null;
  end_at?: string | null;
  source_id: string;
  source: string;
  category: string;
  time_precision?: string;
  metadata?: Record<string, unknown>;
  provenance?: Provenance[];
  media?: MediaAttachment[];
}
export interface MediaAttachment {
  file_id: string;
  url: string;
  mime: string;
  kind: string;
  caption?: string | null;
}
export interface MapLocation {
  id: string;
  event_id: string;
  latitude: number;
  longitude: number;
  occurred_at: string | null;
  title: string;
  source: string;
  kind: string;
  time_precision?: string;
  metadata?: { time_basis?: string; [key: string]: unknown };
}
export interface Provenance {
  file_id: string;
  path: string;
  sha256?: string;
  locator?: unknown;
}
export interface Source {
  id: string;
  name: string;
  status: string;
  file_count: number;
  event_count: number;
  bytes: number;
  first_event?: string | null;
  last_event?: string | null;
  notes?: string | string[] | null;
}
export interface ImportRun {
  id: string;
  source: string;
  status: string;
  total_files: number;
  completed_files: number;
  total_bytes: number;
  completed_bytes: number;
  events: number;
  error?: string | null;
  started_at?: string;
  finished_at?: string | null;
}
export interface Job {
  id: string;
  kind: string;
  status: string;
  prompt: string;
  result?: unknown;
  error?: string | null;
  created_at: string;
  can_undo?: boolean;
}
export interface Overview {
  total_events: number;
  sources_count: number;
  files_count: number;
  stored_bytes: number;
  date_range: { start: string | null; end: string | null };
  activity: { month: string; count: number }[];
  categories: { name: string; count: number }[];
  undated_count?: number;
}
export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

let tokenPromise: Promise<string> | undefined;
async function sessionToken(): Promise<string> {
  if (!tokenPromise) {
    tokenPromise = fetch("/api/session")
      .then(async (response) => {
        if (!response.ok)
          throw new Error(
            "Could not initialize the local session. Refresh and try again.",
          );
        const session = await response.json();
        if (typeof session.token !== "string")
          throw new Error("The local session did not return an access token.");
        return session.token as string;
      })
      .catch((error) => {
        tokenPromise = undefined;
        throw error;
      });
  }
  return tokenPromise;
}
export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const mutation =
    options?.method &&
    !["GET", "HEAD", "OPTIONS"].includes(options.method.toUpperCase());
  const token = mutation ? await sessionToken() : undefined;
  const response = await fetch(`/api${path}`, {
    ...options,
    headers: {
      ...(options?.body ? { "Content-Type": "application/json" } : {}),
      ...(token ? { "X-Biography-Token": token } : {}),
      ...options?.headers,
    },
  });
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const error = await response.json();
      if (typeof error.detail === "string") message = error.detail;
      else if (typeof error.error === "string") message = error.error;
    } catch {
      /* Keep the HTTP error for non-JSON responses. */
    }
    throw new Error(message);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export function formatDate(value?: string | null, precision?: string): string {
  if (!value) return "Date unknown";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  if (precision === "year")
    return date.toLocaleDateString("en-US", {
      year: "numeric",
      timeZone: "UTC",
    });
  if (precision === "month")
    return date.toLocaleDateString("en-US", {
      month: "long",
      year: "numeric",
      timeZone: "UTC",
    });
  return date.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    timeZone: "UTC",
  });
}
export function bytes(value: number): string {
  if (!value) return "0 B";
  const unit = Math.min(Math.floor(Math.log(value) / Math.log(1024)), 4);
  return `${(value / 1024 ** unit).toLocaleString("en-US", { maximumFractionDigits: unit ? 1 : 0 })} ${["B", "KB", "MB", "GB", "TB"][unit]}`;
}
export function count(value?: number): string {
  return (value ?? 0).toLocaleString("en-US");
}
export function humanize(value: string): string {
  return value
    .replace(/[_-]/g, " ")
    .replace(/^\w/, (char) => char.toUpperCase());
}
