export interface LifeEvent {
  id: string;
  title: string;
  body: string;
  occurred_at: string | null;
  end_at?: string | null;
  source_id: string;
  source: string;
  category: string;
  source_label?: string;
  display_category?: string;
  display_class?: string;
  is_raw_sample?: boolean;
  classification?: Record<string, unknown>;
  metric?: Record<string, unknown> | null;
  metrics?: Record<string, unknown>[];
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
export interface ProjectionStatus {
  status?: string;
  watermark?: number | null;
  current_catalog_max_rowid?: number | null;
  catalog_max_event_rowid?: number | null;
  [key: string]: unknown;
  watermark_rowid?: number | null;
  processed_records?: number;
  caught_up_to_watermark?: boolean;
  append_backlog_rowid_span?: number;
  rebuild_required?: boolean;
  updated_at?: string | null;
}
export interface DaySummary {
  day: string;
  date: string | null;
  observed_records: number;
  raw_samples: number;
  substantive_records: number;
  technical_records: number;
  display_groups: number;
  measurement_groups: number;
  source_count: number;
  date_bases: { basis: string; count: number }[];
}
export interface DailyRecord extends LifeEvent {
  duplicate_count?: number;
  display_class?: string;
  date_basis?: string;
  source_label?: string;
  source_record_count?: number;
}
export interface DailyRollup {
  id: string;
  day: string;
  source_id: string;
  source: string;
  category: string;
  metric: string;
  unit: string | null;
  device?: string | null;
  aggregation: string;
  sample_count: number;
  numeric_count: number;
  min: number | null;
  max: number | null;
  sample_mean: number | null;
  sum: number | null;
  latest: number | null;
  first_at: string | null;
  last_at: string | null;
  evidence_ids: string[];
  watermark: unknown;
  source_record_count?: number;
  duplicate_count?: number;
  date_basis?: string;
  notes?: string[];
}
export interface DaysPage extends Page<DaySummary> {
  projection: ProjectionStatus;
  q_scope?: string;
}
export interface DayDetail extends Page<DailyRecord> {
  day: string;
  summary: DaySummary;
  rollups: DailyRollup[];
  projection: ProjectionStatus;
  rollups_total?: number;
  rollups_truncated?: boolean;
}
export interface RollupPage extends Page<DailyRecord> {
  rollup: DailyRollup;
  projection: ProjectionStatus;
}
export interface CoverageSource {
  id: string;
  name: string;
  status: string;
  queue_order: number;
  notes: string[];
  files: {
    total: number;
    bytes: number;
    by_parse_status: Record<string, number>;
    parsed: number;
    partial: number;
    metadata_only: number;
    retained_only: number;
    empty: number;
    excluded: number;
    pending: number;
    needs_review: number;
  };
  records: {
    status: string;
    total: number | null;
    dated: number | null;
    undated: number | null;
    first_observed: string | null;
    last_observed: string | null;
    measured_at: string | null;
  };
  extraction: {
    review_files: number;
    pending_files: number;
    history_absence_established: boolean;
  };
  classification: { status: string; label: string };
  latest_import?: {
    id: string;
    status: string;
    started_at: string;
    finished_at: string | null;
    error: SafeImportError;
  };
}
export type SafeImportError = string | { code: string; message: string } | null;
export interface CoverageReport {
  scope: string;
  continuous_coverage_asserted: boolean;
  generated_at: string;
  totals: {
    sources: number;
    files: number;
    bytes: number;
    parsed_files: number;
    metadata_only_files: number;
    retained_only_files: number;
    review_files: number;
    events: number | null;
    dated_events: number | null;
    undated_events: number | null;
  };
  sources: CoverageSource[];
  active_imports: {
    id: string;
    source_id: string;
    source: string;
    status: string;
    files: { completed: number; total: number; fraction: number | null };
    bytes: { completed: number; total: number; fraction: number | null };
    events_reported: number;
    started_at: string;
    error: SafeImportError;
  }[];
  queue: {
    source_id: string;
    source: string;
    status: string;
    position: number;
    snapshot_bytes: number;
    error?: SafeImportError;
  }[];
  import_issues?: {
    id: string;
    source_id: string;
    source: string;
    status: string;
    error: SafeImportError;
  }[];
  indexing: {
    catalog_max_event_rowid: number;
    coverage_stats: {
      status: string;
      catalog_max_event_rowid: number | null;
      measured_at: string | null;
    };
    daily: ProjectionStatus;
  };
  backup: { status: string; [key: string]: unknown };
}
export interface SummaryCoverage {
  total_events_in_scope: number;
  selected_events: number;
  candidate_events: number;
  undated_events_excluded: number;
  sources: {
    source_id: string;
    source: string;
    total_events: number;
    selected_events: number;
    status: string;
  }[];
  retrieval_limited: boolean;
  time_basis: string;
  limitations: string[];
}
export interface PeriodSummary {
  kind: "period_summary";
  period: string;
  title: string;
  answer: string;
  citations: string[];
  sections: {
    heading: string;
    sentences: { text: string; citations: string[] }[];
  }[];
  coverage: SummaryCoverage;
  selection: unknown;
  model: string;
  cache: {
    status: "fresh" | "stale";
    key: string;
    generated_at: string;
    fixed_watermark: string;
    current_watermark: string;
  };
  limitations: string[];
}
export interface SummaryCache {
  status: "missing" | "fresh" | "stale";
  period: string;
  key: string;
  model: string;
  fixed_watermark: string | null;
  current_watermark: string;
  current_coverage: SummaryCoverage;
  result: PeriodSummary | null;
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
