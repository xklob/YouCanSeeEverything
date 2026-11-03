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
  event_count: number | null;
  bytes: number;
  first_event?: string | null;
  last_event?: string | null;
  notes?: string | string[] | null;
  stats?: StatsSnapshot;
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
  total_events: number | null;
  sources_count: number | null;
  files_count: number | null;
  stored_bytes: number | null;
  date_range: { start: string | null; end: string | null };
  activity: { month: string; count: number | null }[];
  categories: { name: string; count: number | null }[];
  undated_count?: number | null;
  stats?: StatsSnapshot;
}
export type TotalStatus =
  "current" | "cached" | "stale" | "pending" | "counting" | "unavailable";
export interface StatsSnapshot {
  status: TotalStatus | "partial" | "ready" | "refreshing";
  measured_at: string | null;
  watermark?: unknown;
  refreshing?: boolean;
  activity_status?: string;
  categories_status?: string;
  error?: unknown;
  counted_events?: number | null;
}
export interface Page<T> {
  items: T[];
  total: number | null;
  limit: number;
  offset: number;
  has_more?: boolean;
  total_status?: TotalStatus;
  counted_at?: string | null;
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
  occurrence_count?: number;
  variant_count?: number;
  conflicting_occurrences?: number;
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
  total: number;
  projection: ProjectionStatus;
  q_scope?: string;
}
export interface DailyCategories {
  items: { name: string; category: string; count: number }[];
  projection: ProjectionStatus;
}
export interface DayDetail extends Page<DailyRecord> {
  total: number;
  day: string;
  summary: DaySummary;
  rollups: DailyRollup[];
  projection: ProjectionStatus;
  rollups_total?: number;
  rollups_truncated?: boolean;
}
export interface RollupPage extends Page<DailyRecord> {
  total: number;
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
    counted_events?: number | null;
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
    counted_events?: number | null;
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
export interface TripSummaryContext {
  trip_id: string; source_id: string; source: string;
  departure_at: string; arrival_at: string; window_start: string; window_end: string;
  time_basis: string; notice: string; coincident_notice: string;
  anchor_event_ids: string[]; coincident_event_ids: string[];
  anchors_complete: boolean; journey_coverage_complete: boolean;
}
export type SavedNarrative = Omit<PeriodSummary, "kind" | "period"> & {
  kind: "period_summary" | "trip_summary"; period?: string | null;
  trip_context?: TripSummaryContext;
};
export interface SavedSummaryEntry {
  key: string; kind: SavedNarrative["kind"]; title: string; period: string | null;
  trip_id: string | null; focus: string; model: string; source_ids: string[];
  generated_at: string; citation_count: number; evidence_count: number;
  freshness: "not_checked";
}
export interface SavedSummaryPage { items: SavedSummaryEntry[]; limit: number; offset: number; has_more: boolean }
export interface SavedSummaryResult { status: "saved" | "missing"; freshness?: "not_checked"; result: SavedNarrative | null }

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
export function bytes(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0)
    return "Unknown";
  if (value === 0) return "0 B";
  const unit = Math.min(Math.floor(Math.log(value) / Math.log(1024)), 4);
  return `${(value / 1024 ** unit).toLocaleString("en-US", { maximumFractionDigits: unit ? 1 : 0 })} ${["B", "KB", "MB", "GB", "TB"][unit]}`;
}
export function count(value?: number | null): string {
  return typeof value === "number" && Number.isFinite(value)
    ? value.toLocaleString("en-US")
    : "Unknown";
}

export function recordCount(value?: number | null, stats?: {status: string; counted_events?: number | null}): string {
  if (typeof value === "number" && Number.isFinite(value)) return count(value);
  if (stats?.status === "counting")
    return typeof stats.counted_events === "number" && stats.counted_events > 0
      ? `${count(stats.counted_events)}+ (counting)` : "Counting…";
  return stats?.status === "pending" || stats?.status === "refreshing" ? "Counting…" : "Count unavailable";
}
export function hasMore(
  page: Pick<Page<unknown>, "total" | "items" | "offset" | "has_more">,
): boolean {
  return typeof page.has_more === "boolean"
    ? page.has_more
    : typeof page.total === "number" &&
        page.offset + page.items.length < page.total;
}
export function shownRange(
  page: Pick<Page<unknown>, "items" | "offset" | "total" | "total_status">,
): string {
  if (!page.items.length) return "No records shown";
  const start = page.items.length ? page.offset + 1 : 0;
  const end = page.offset + page.items.length;
  return `${count(start)}–${count(end)}${typeof page.total === "number" ? (page.total_status === "cached" || page.total_status === "stale" ? ` · cached total ${count(page.total)}` : ` of ${count(page.total)}`) : " shown"}`;
}
export function humanize(value: string): string {
  return value
    .replace(/[_-]/g, " ")
    .replace(/^\w/, (char) => char.toUpperCase());
}
