import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  ArrowRight,
  ArrowUpRight,
  BookOpen,
  CalendarDays,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  CircleAlert,
  Clock3,
  Database,
  FileText,
  FolderArchive,
  HardDrive,
  Layers3,
  List,
  LoaderCircle,
  LockKeyhole,
  Menu,
  Map as MapIcon,
  Route,
  MessageSquareText,
  RefreshCw,
  Search,
  Send,
  SlidersHorizontal,
  Sparkles,
  Undo2,
  X,
} from "lucide-react";
import {
  api,
  bytes,
  count,
  recordCount,
  formatDate,
  humanize,
  hasMore,
  shownRange,
  type ImportRun,
  type Job,
  type LifeEvent,
  type Overview,
  type Page,
  type Source,
  type DailyCategories,
  type StatsSnapshot,
  type SavedNarrative,
} from "./api";
import MapWorkspace from "./MapWorkspace";
import MediaGallery from "./MediaGallery";
import EnrichmentEvidence, { EnrichedLocation } from "./EnrichmentEvidence";
import DailyWorkspace from "./DailyWorkspace";
import CoverageWorkspace from "./CoverageWorkspace";
import ReadableData, { CitedProse, readablePreview } from "./ReadableData";
import TripsWorkspace from "./TripsWorkspace";
import SavedSummaries from "./SavedSummaries";
import SummaryView from "./SummaryView";
import {
  SortControls,
  sortItems,
  sourceSortOptions,
  useSortPreference,
  type SortPreference,
} from "./sorting";

type View = "timeline" | "sources" | "coverage" | "assistant" | "trips";
type Presentation = "map" | "daily" | "timeline";
interface TripMapRequest {
  from: string;
  to: string;
  sourceId: string;
  tripId: string;
  key: number;
}
const PAGE_SIZE = 40;
const activeStatuses = [
  "queued",
  "pending",
  "running",
  "importing",
  "processing",
  "indexing",
];

function ErrorNotice({
  message,
  retry,
}: {
  message: string;
  retry?: () => void;
}) {
  return (
    <div className="notice error" role="alert">
      <CircleAlert size={17} />
      <span>{message}</span>
      {retry && <button onClick={retry}>Try again</button>}
    </div>
  );
}
function EmptyState({
  icon,
  title,
  children,
}: {
  icon: ReactNode;
  title: string;
  children: ReactNode;
}) {
  return (
    <div className="empty-state">
      <div className="empty-icon">{icon}</div>
      <h3>{title}</h3>
      <p>{children}</p>
    </div>
  );
}
function Status({ value }: { value: string }) {
  const active = activeStatuses.includes(value.toLowerCase());
  const failed = ["failed", "error", "cancelled"].includes(value.toLowerCase());
  return (
    <span className={`status ${active ? "active" : failed ? "failed" : ""}`}>
      {active ? (
        <LoaderCircle size={12} className="spin" />
      ) : failed ? (
        <CircleAlert size={12} />
      ) : (
        <span className="status-dot" />
      )}
      {humanize(value)}
    </span>
  );
}
function CountFreshness({ stats }: { stats?: StatsSnapshot }) {
  if (!stats || stats.status === "current" || stats.status === "ready")
    return null;
  const text =
    stats.status === "counting"
      ? "Counting existing records"
      : stats.status === "cached" || stats.status === "stale"
      ? "Cached counts"
      : stats.status === "partial"
        ? "Partial counts"
        : stats.status === "refreshing"
          ? "Counts updating"
          : stats.status === "pending"
            ? "Counts pending"
            : "Counts unavailable";
  return (
    <span
      className="count-freshness"
      title={
        stats.measured_at
          ? `Measured ${formatDate(stats.measured_at)}`
          : undefined
      }
    >
      {text}
    </span>
  );
}

export default function App() {
  const [view, setView] = useState<View>("timeline");
  const [presentation, setPresentation] = useState<Presentation>("map");
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [overview, setOverview] = useState<Overview | null>(null);
  const [sources, setSources] = useState<Source[]>([]);
  const [imports, setImports] = useState<ImportRun[]>([]);
  const [libraryError, setLibraryError] = useState("");
  const [libraryLoading, setLibraryLoading] = useState(true);
  const [refreshKey, setRefreshKey] = useState(0);
  const [sourceFilter, setSourceFilter] = useState("");
  const [tripMapRequest, setTripMapRequest] = useState<TripMapRequest | null>(
    null,
  );
  const [tripEvent, setTripEvent] = useState<string | null>(null);
  const refresh = useCallback(() => setRefreshKey((value) => value + 1), []);

  useEffect(() => {
    let live = true;
    let inFlight = false;
    let timer: number | undefined;
    const controller = new AbortController();
    const load = async () => {
      if (!live || inFlight) return;
      inFlight = true;
      try {
        const responses = await Promise.allSettled([
          api<Overview>("/overview", { signal: controller.signal }),
          api<{ items: Source[] }>("/sources", { signal: controller.signal }),
          api<{ items: ImportRun[] }>("/imports", {
            signal: controller.signal,
          }),
        ]);
        if (!live) return;
        const [summary, library, progress] = responses;
        if (summary.status === "fulfilled") setOverview(summary.value);
        if (library.status === "fulfilled") setSources(library.value.items);
        if (progress.status === "fulfilled") setImports(progress.value.items);
        const errors = responses.filter(
          (result) => result.status === "rejected",
        );
        setLibraryError(
          errors.length
            ? "Some archive information could not be loaded. Check that the local server is running."
            : "",
        );
        setLibraryLoading(false);
      } finally {
        inFlight = false;
        if (live)
          timer = window.setTimeout(() => {
            void load();
          }, 30000);
      }
    };
    void load();
    return () => {
      live = false;
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [refreshKey]);

  const navigate = (next: View) => {
    setView(next);
    setSidebarOpen(false);
  };
  const browseSource = (id: string) => {
    setTripMapRequest(null);
    setSourceFilter(id);
    setPresentation("timeline");
    navigate("timeline");
  };
  const runningImports = imports.filter((item) =>
    activeStatuses.includes(item.status.toLowerCase()),
  );

  return (
    <div className="app-shell">
      {sidebarOpen && (
        <button
          className="sidebar-scrim"
          aria-label="Close navigation"
          onClick={() => setSidebarOpen(false)}
        />
      )}
      <aside
        className={`sidebar ${sidebarOpen ? "open" : ""}`}
        aria-label="Main navigation"
      >
        <a
          className="brand"
          href="#timeline"
          onClick={(event) => {
            event.preventDefault();
            setTripMapRequest(null);
            navigate("timeline");
          }}
        >
          <div className="brand-mark">
            <BookOpen size={23} strokeWidth={1.6} />
          </div>
          <span>
            Biography<span className="brand-secondary">of you</span>
          </span>
        </a>
        <div className="workspace-label">LOCAL ARCHIVE</div>
        <nav>
          <button
            className={`nav-item ${view === "timeline" ? "selected" : ""}`}
            onClick={() => {
              setTripMapRequest(null);
              navigate("timeline");
            }}
          >
            <MapIcon size={18} />
            <span>Records</span>
            {typeof overview?.total_events === "number" && (
              <span className="nav-count">{count(overview.total_events)}</span>
            )}
          </button>
          <button
            className={`nav-item ${view === "trips" ? "selected" : ""}`}
            onClick={() => navigate("trips")}
          >
            <Route size={18} />
            <span>Trips</span>
          </button>
          <button
            className={`nav-item ${view === "sources" ? "selected" : ""}`}
            onClick={() => navigate("sources")}
          >
            <FolderArchive size={18} />
            <span>Sources</span>
            {runningImports.length > 0 && <span className="activity-dot" />}
          </button>
          <button
            className={`nav-item ${view === "coverage" ? "selected" : ""}`}
            onClick={() => navigate("coverage")}
          >
            <SlidersHorizontal size={18} />
            <span>Imports</span>
          </button>
          <button
            className={`nav-item ${view === "assistant" ? "selected" : ""}`}
            onClick={() => navigate("assistant")}
          >
            <Sparkles size={18} />
            <span>Ask</span>
            <span className="small-badge">CODEX</span>
          </button>
        </nav>
        <div className="sidebar-divider" />
        <div className="sidebar-section-heading">
          <span>SOURCES</span>
          <span>{sources.length || ""}</span>
        </div>
        <div className="source-shortcuts">
          {sources.slice(0, 9).map((source, index) => (
            <button
              key={source.id}
              onClick={() => browseSource(source.id)}
              className={
                view === "timeline" && sourceFilter === source.id
                  ? "source-selected"
                  : ""
              }
            >
              <span className={`source-glyph color-${index % 5}`}>
                <Database size={13} />
              </span>
              <span>{source.name}</span>
              {typeof source.event_count === "number" && (
                <span
                  className="source-shortcut-count"
                  title={
                    source.stats?.status === "stale" ||
                    source.stats?.status === "cached"
                      ? "Cached count"
                      : undefined
                  }
                >
                  {recordCount(source.event_count, source.stats)}
                </span>
              )}
            </button>
          ))}
          {!sources.length && (
            <p className="sidebar-hint">
              {libraryLoading
                ? "Loading your sources…"
                : "Imported sources appear here."}
            </p>
          )}
          {sources.length > 9 && (
            <button className="view-all" onClick={() => navigate("sources")}>
              View all sources <ArrowRight size={13} />
            </button>
          )}
        </div>
        <div className="sidebar-bottom">
          {runningImports.length > 0 && (
            <button
              className="import-indicator"
              onClick={() => navigate("coverage")}
            >
              <LoaderCircle size={15} className="spin" />
              <span>
                {runningImports.length} import
                {runningImports.length === 1 ? "" : "s"} in progress
              </span>
              <ChevronRight size={14} />
            </button>
          )}
          <div className="local-note">
            <span className="local-led" />
            <span>Local & private</span>
            <LockKeyhole size={12} />
          </div>
          <p>Served on localhost.</p>
        </div>
      </aside>
      <main className="main-area">
        <header className="topbar">
          <div className="breadcrumb">
            <button
              className="mobile-menu icon-button"
              aria-label="Open navigation"
              onClick={() => setSidebarOpen(true)}
            >
              <Menu size={20} />
            </button>
            <span>Archive</span>
            <ChevronRight size={13} />
            <strong>
              {view === "timeline"
                ? "Records"
                : view === "sources"
                  ? "Sources"
                  : view === "trips"
                    ? "Trips"
                    : view === "coverage"
                      ? "Imports"
                      : "Ask"}
            </strong>
          </div>
          <div className="topbar-right">
            <span>
              <span className="local-led" /> LOCAL WORKSPACE
            </span>
            <button
              className="icon-button"
              aria-label="Refresh archive"
              title="Refresh archive"
              onClick={refresh}
            >
              <RefreshCw size={16} />
            </button>
          </div>
        </header>
        {view === "timeline" && (
          <Timeline
            overview={overview}
            sources={sources}
            sourceFilter={sourceFilter}
            setSourceFilter={setSourceFilter}
            refreshKey={refreshKey}
            onShowSources={() => navigate("sources")}
            presentation={presentation}
            setPresentation={setPresentation}
            tripMapRequest={tripMapRequest}
          />
        )}
        {view === "sources" && (
          <SourceLibrary
            overview={overview}
            sources={sources}
            imports={imports}
            loading={libraryLoading}
            error={libraryError}
            refresh={refresh}
            browseSource={browseSource}
          />
        )}
        {view === "assistant" && <Assistant onChange={refresh} />}
        {view === "trips" && (
          <TripsWorkspace
            refreshKey={refreshKey}
            onOpenEvent={setTripEvent}
            onShowMap={(request) => {
              setTripMapRequest({ ...request, key: Date.now() });
              setSourceFilter(request.sourceId);
              setPresentation("map");
              navigate("timeline");
            }}
          />
        )}
        {view === "coverage" && (
          <CoverageWorkspace
            refreshKey={refreshKey}
            onBrowseSource={browseSource}
          />
        )}
        {tripEvent && (
          <EventDrawer id={tripEvent} close={() => setTripEvent(null)} />
        )}
        <footer className="page-footer">
          <BookOpen size={13} />
          <span>Biography of You</span>
          <span className="footer-separator">·</span>
          <span>Local archive.</span>
        </footer>
      </main>
    </div>
  );
}

function Timeline({
  overview,
  sources,
  sourceFilter,
  setSourceFilter,
  refreshKey,
  onShowSources,
  presentation,
  setPresentation,
  tripMapRequest,
}: {
  overview: Overview | null;
  sources: Source[];
  sourceFilter: string;
  setSourceFilter: (value: string) => void;
  refreshKey: number;
  onShowSources: () => void;
  presentation: Presentation;
  setPresentation: (value: Presentation) => void;
  tripMapRequest: TripMapRequest | null;
}) {
  const [query, setQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [category, setCategory] = useState("");
  const [dailyCategories, setDailyCategories] = useState<
    DailyCategories["items"]
  >([]);
  const [dailyCategoryLoading, setDailyCategoryLoading] = useState(false);
  const [dailyCategoryError, setDailyCategoryError] = useState("");
  const categoryDomain = presentation === "daily" ? "derived" : "raw";
  const [previousCategoryDomain, setPreviousCategoryDomain] =
    useState(categoryDomain);
  if (previousCategoryDomain !== categoryDomain) {
    setPreviousCategoryDomain(categoryDomain);
    setCategory("");
  }
  const [from, setFrom] = useState(tripMapRequest?.from || "");
  const [to, setTo] = useState(tripMapRequest?.to || "");
  const [undated, setUndated] = useState(false);
  const recordSort = useSortPreference("records", "date", ["date", "imported"], "desc");
  const [sort, direction] = recordSort;
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<Page<LifeEvent> | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const searchInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (presentation !== "daily") return;
    const controller = new AbortController();
    setDailyCategoryLoading(true);
    setDailyCategoryError("");
    api<DailyCategories>("/daily/categories", { signal: controller.signal })
      .then((result) => {
        if (!controller.signal.aborted) setDailyCategories(result.items);
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setDailyCategoryError(cause.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setDailyCategoryLoading(false);
      });
    return () => controller.abort();
  }, [presentation, refreshKey, retry]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement;
      if (
        event.key === "/" &&
        !["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName) &&
        !target.isContentEditable
      ) {
        event.preventDefault();
        searchInput.current?.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedQuery(query), 300);
    return () => window.clearTimeout(timer);
  }, [query]);
  // Reset pagination in the same render as filter changes to avoid showing a stale page.
  const filterKey = [
    debouncedQuery,
    category,
    sourceFilter,
    from,
    to,
    sort,
    direction,
    undated,
  ].join("\0");
  const [previousFilters, setPreviousFilters] = useState(filterKey);
  if (previousFilters !== filterKey) {
    setPreviousFilters(filterKey);
    setOffset(0);
  }

  useEffect(() => {
    if (presentation !== "timeline") return;
    const controller = new AbortController();
    setLoading(true);
    setError("");
    const params = new URLSearchParams({
      limit: String(PAGE_SIZE),
      offset: String(offset),
      sort,
      direction,
    });
    if (debouncedQuery) params.set("q", debouncedQuery);
    if (sourceFilter) params.set("source", sourceFilter);
    if (category) params.set("category", category);
    if (from) params.set("from", from);
    if (to) params.set("to", to);
    if (undated) params.set("undated", "true");
    api<Page<LifeEvent>>(`/events?${params}`, { signal: controller.signal })
      .then((result) => {
        if (!controller.signal.aborted) setPage(result);
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setError(cause.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [
    debouncedQuery,
    sourceFilter,
    category,
    from,
    to,
    sort,
    direction,
    undated,
    offset,
    refreshKey,
    retry,
    presentation,
  ]);

  const grouped = useMemo(() => {
    if (sort === "imported") return page?.items.length ? [{key: "imported", label: "Import order", items: page.items}] : [];
    const groups: { key: string; label: string; items: LifeEvent[] }[] = [];
    for (const event of page?.items ?? []) {
      const key = event.occurred_at?.slice(0, 7) || "unknown";
      let group = groups.find((item) => item.key === key);
      if (!group) {
        group = {
          key,
          label:
            key === "unknown"
              ? "Undated"
              : formatDate(
                  event.occurred_at,
                  event.time_precision === "year" ? "year" : "month",
                ),
          items: [],
        };
        groups.push(group);
      }
      group.items.push(event);
    }
    return groups;
  }, [page, sort]);
  const years = useMemo(
    () =>
      [
        ...new Set(
          (overview?.activity ?? []).map((item) => item.month.slice(0, 4)),
        ),
      ]
        .sort()
        .reverse(),
    [overview],
  );
  const activeFilterCount = [
    sourceFilter,
    category,
    from || to,
    undated,
  ].filter(Boolean).length;
  const clearFilters = () => {
    setQuery("");
    setSourceFilter("");
    setCategory("");
    setFrom("");
    setTo("");
    setUndated(false);
  };
  const setYear = (year: string) => {
    setFrom(`${year}-01-01`);
    setTo(`${year}-12-31`);
    setUndated(false);
  };

  return (
    <div className="page timeline-page">
      <div className="page-heading">
        <div>
          <h1>Records</h1>
          <p>Search imported records.</p>
        </div>
      </div>
      <div className="archive-summary">
        <span>
          Records:{" "}
          <span className="summary-number">
            {overview ? recordCount(overview.total_events, overview.stats) : "—"}
          </span>
        </span>
        <i />
        <span>
          <span className="summary-number">
            {overview ? count(overview.sources_count) : "—"}
          </span>{" "}
          sources
        </span>
        <i />
        <span>
          <CalendarDays size={14} />
          {overview?.date_range &&
          (overview.date_range.start || overview.date_range.end)
            ? `${formatDate(overview.date_range.start, "year")} – ${formatDate(overview.date_range.end, "year")}`
            : overview?.stats?.status === "current" ||
                overview?.stats?.status === "ready"
              ? "No dated records"
              : "Dates pending"}
        </span>
        <CountFreshness stats={overview?.stats} />
        <button onClick={onShowSources}>
          View sources <ArrowUpRight size={14} />
        </button>
      </div>
      <section
        className="timeline-toolbar"
        aria-label="Search and filter timeline"
      >
        <div className="search-row">
          <label className="search-field">
            <Search size={19} />
            <input
              ref={searchInput}
              aria-label="Search your timeline"
              type="search"
              placeholder="Search records…"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
            <kbd>/</kbd>
          </label>
          <button
            className={`filter-toggle ${activeFilterCount ? "has-filters" : ""}`}
            onClick={() => setFiltersOpen((value) => !value)}
            aria-expanded={filtersOpen}
          >
            <SlidersHorizontal size={16} />
            <span>Filters</span>
            {activeFilterCount > 0 && <b>{activeFilterCount}</b>}
            <ChevronDown size={13} className={filtersOpen ? "rotated" : ""} />
          </button>
        </div>
        <div className={`filter-row ${filtersOpen ? "expanded" : ""}`}>
          <label>
            <Database size={14} />
            <select
              aria-label="Filter by source"
              value={sourceFilter}
              onChange={(event) => setSourceFilter(event.target.value)}
            >
              <option value="">All sources</option>
              {sources.map((source) => (
                <option key={source.id} value={source.id}>
                  {source.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            <Layers3 size={14} />
            <select
              aria-label="Filter by type"
              aria-busy={presentation === "daily" && dailyCategoryLoading}
              value={category}
              onChange={(event) => setCategory(event.target.value)}
            >
              <option value="">All types</option>
              {(presentation === "daily"
                ? dailyCategories
                : (overview?.categories ?? [])
              ).map((item) => (
                <option key={item.name} value={item.name}>
                  {humanize(item.name)}
                  {typeof item.count === "number"
                    ? ` (${count(item.count)})`
                    : ""}
                </option>
              ))}
            </select>
          </label>
          {presentation === "daily" && dailyCategoryError && (
            <span className="filter-type-error" role="alert">
              Day types unavailable.{" "}
              <button onClick={() => setRetry((value) => value + 1)}>
                Retry
              </button>
            </span>
          )}
          <div className="date-inputs">
            <CalendarDays size={14} />
            <input
              aria-label="From date"
              type="date"
              disabled={undated}
              value={from}
              max={to || undefined}
              onChange={(event) => setFrom(event.target.value)}
            />
            <span>to</span>
            <input
              aria-label="To date"
              type="date"
              disabled={undated}
              value={to}
              min={from || undefined}
              onChange={(event) => setTo(event.target.value)}
            />
          </div>
          <label className="undated-filter">
            <input
              type="checkbox"
              checked={undated}
              onChange={(event) => {
                setUndated(event.target.checked);
                if (event.target.checked) {
                  setFrom("");
                  setTo("");
                }
              }}
            />
            Undated only
            {overview?.undated_count
              ? ` (${count(overview.undated_count)})`
              : ""}
          </label>
          {(activeFilterCount > 0 || query) && (
            <button className="clear-filters" onClick={clearFilters}>
              <X size={13} />
              Clear
            </button>
          )}
        </div>
      </section>
      <div className="view-switch-row">
        <div className="view-switch" role="group" aria-label="Archive view">
          <button
            className={presentation === "map" ? "selected" : ""}
            onClick={() => setPresentation("map")}
          >
            <MapIcon size={15} />
            Map
          </button>
          <button
            className={presentation === "daily" ? "selected" : ""}
            onClick={() => setPresentation("daily")}
          >
            <CalendarDays size={15} />
            Days
          </button>
          <button
            className={presentation === "timeline" ? "selected" : ""}
            onClick={() => setPresentation("timeline")}
          >
            <List size={15} />
            Records
          </button>
        </div>
        <span>
          {presentation === "map" ? "" : presentation === "daily" ? "" : ""}
        </span>
      </div>
      {presentation === "map" ? (
        <MapWorkspace
          filters={{
            q: debouncedQuery,
            source: sourceFilter,
            category,
            from,
            to,
            undated,
          }}
          refreshKey={refreshKey}
          onOpenEvent={setSelected}
          onShowTimeline={() => setPresentation("timeline")}
          focusTripId={tripMapRequest?.tripId}
        />
      ) : presentation === "daily" ? (
        <DailyWorkspace
          filters={{
            q: debouncedQuery,
            source: sourceFilter,
            category,
            from,
            to,
            undated,
          }}
          refreshKey={refreshKey}
          onOpenEvent={setSelected}
        />
      ) : (
        <div className="timeline-layout">
          <section
            className="timeline-results"
            aria-label="Timeline results"
            aria-busy={loading}
          >
            <div className="results-heading">
              <span>
                {loading
                  ? "Finding your records…"
                  : typeof page?.total === "number"
                    ? `${count(page.total)} ${debouncedQuery || activeFilterCount ? "matching " : ""}records`
                    : `${count(page?.items.length)} shown`}
                {debouncedQuery && !loading && (
                  <span className="query-label"> for “{debouncedQuery}”</span>
                )}
              </span>
              {page?.total_status && page.total_status !== "current" && (
                <CountFreshness
                  stats={{
                    status: page.total_status,
                    measured_at: page.counted_at ?? null,
                  }}
                />
              )}
              <SortControls label="Records" options={[["date", "Date"], ["imported", "Import order"]]} value={recordSort} />
            </div>
            {error && (
              <ErrorNotice
                message={error}
                retry={() => setRetry((value) => value + 1)}
              />
            )}
            {loading ? (
              <div
                className="timeline-skeleton"
                role="status"
                aria-label="Loading timeline"
              >
                {[0, 1, 2].map((index) => (
                  <div key={index} className="skeleton-card">
                    <span />
                    <div />
                    <div />
                    <div />
                  </div>
                ))}
              </div>
            ) : !error && page?.items.length === 0 ? (
              <EmptyState
                icon={
                  debouncedQuery || activeFilterCount ? (
                    <Search size={27} />
                  ) : (
                    <BookOpen size={27} />
                  )
                }
                title={
                  debouncedQuery || activeFilterCount
                    ? "No matching records"
                    : "No records"
                }
              >
                {debouncedQuery || activeFilterCount ? (
                  <>
                    Try a different search or{" "}
                    <button className="inline-button" onClick={clearFilters}>
                      clear your filters
                    </button>
                    .
                  </>
                ) : (
                  <>
                    Records appear as imports finish.{" "}
                    <button className="inline-button" onClick={onShowSources}>
                      Open Sources
                    </button>
                    .
                  </>
                )}
              </EmptyState>
            ) : (
              !error && (
                <div className="timeline-groups">
                  {grouped.map((group) => (
                    <div className="timeline-group" key={group.key}>
                      <div className="month-heading">
                        <span className="timeline-node" />
                        <h2>{group.label}</h2>
                        <span className="month-rule" />
                        <span>{count(group.items.length)}</span>
                      </div>
                      <div className="month-events">
                        {group.items.map((event) => (
                          <EventCard
                            key={event.id}
                            event={event}
                            onClick={() => setSelected(event.id)}
                            sourceIndex={sources.findIndex(
                              (source) => source.id === event.source_id,
                            )}
                          />
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              )
            )}
            {!loading && !error && page && (offset > 0 || hasMore(page)) && (
              <div className="pagination">
                <span>{shownRange(page)}</span>
                <div>
                  <button
                    disabled={offset === 0}
                    onClick={() => {
                      setOffset((value) => Math.max(0, value - PAGE_SIZE));
                      window.scrollTo({ top: 0, behavior: "smooth" });
                    }}
                  >
                    <ChevronLeft size={16} />
                    Previous
                  </button>
                  <button
                    disabled={!hasMore(page)}
                    onClick={() => {
                      setOffset((value) => value + PAGE_SIZE);
                      window.scrollTo({ top: 0, behavior: "smooth" });
                    }}
                  >
                    Next
                    <ChevronRight size={16} />
                  </button>
                </div>
              </div>
            )}
          </section>
          <aside className="time-rail">
            <div className="rail-heading">YEARS</div>
            {years.length ? (
              <div className="year-list">
                <button
                  className={!from && !to ? "current" : ""}
                  onClick={() => {
                    setFrom("");
                    setTo("");
                  }}
                >
                  <span>All time</span>
                  <span className="year-dot" />
                </button>
                {years.map((year) => (
                  <button
                    key={year}
                    className={
                      from === `${year}-01-01` && to === `${year}-12-31`
                        ? "current"
                        : ""
                    }
                    onClick={() => setYear(year)}
                  >
                    <span>{year}</span>
                    {typeof overview?.total_events === "number" &&
                      overview.total_events > 0 &&
                      overview.activity.every(
                        (item) => typeof item.count === "number",
                      ) && (
                        <span
                          className="year-activity"
                          style={{
                            width: `${Math.max(8, Math.min(50, ((overview?.activity.filter((item) => item.month.startsWith(year)).reduce((sum, item) => sum + (item.count ?? 0), 0) ?? 0) / Math.max(1, overview?.total_events ?? 1)) * 150))}px`,
                          }}
                        />
                      )}
                  </button>
                ))}
              </div>
            ) : (
              <p className="rail-empty">
                {overview?.stats?.activity_status === "not_computed"
                  ? "Use date filters."
                  : overview?.stats?.status === "current" ||
                      overview?.stats?.status === "ready"
                    ? "No indexed years."
                    : "Year counts pending."}
              </p>
            )}
            <div className="rail-note">
              <BookOpen size={19} strokeWidth={1.4} />
              <p>Source evidence</p>
              <span>Open a record for its source.</span>
            </div>
          </aside>
        </div>
      )}
      {selected && (
        <EventDrawer id={selected} close={() => setSelected(null)} />
      )}
    </div>
  );
}

function EventCard({
  event,
  onClick,
  sourceIndex,
}: {
  event: LifeEvent;
  onClick: () => void;
  sourceIndex: number;
}) {
  return (
    <article className="event-card" data-record-id={event.id}>
      <button className="event-open-button" onClick={onClick}>
        <div className="event-card-top">
          <span
            className={`event-source color-${Math.max(0, sourceIndex) % 5}`}
          >
            <span className="source-dot" />
            {event.source_label || event.source || "Unknown source"}
          </span>
          <span className="event-date">
            {formatDate(event.occurred_at, event.time_precision)}
          </span>
        </div>
        <h3>{event.title || "Untitled record"}</h3>
        <EnrichedLocation event={event} />
        {event.body && (
          <p className="event-excerpt">{readablePreview(event.body)}</p>
        )}
        <div className="event-card-bottom">
          <span className="category-tag">
            {humanize(event.display_category || event.category || "record")}
          </span>
          {event.time_precision &&
            !["day", "second", "exact"].includes(event.time_precision) && (
              <span className="precision-note">
                <Clock3 size={11} />
                {humanize(event.time_precision)} precision
              </span>
            )}
          <span className="open-record">
            Open record <ArrowUpRight size={13} />
          </span>
        </div>
      </button>
      <MediaGallery media={event.media} compact />
    </article>
  );
}

function EventDrawer({ id, close }: { id: string; close: () => void }) {
  const [event, setEvent] = useState<LifeEvent | null>(null);
  const [error, setError] = useState("");
  const closeButton = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const before = document.activeElement as HTMLElement | null;
    closeButton.current?.focus();
    const keydown = (key: KeyboardEvent) => {
      if (key.key === "Escape") close();
      if (key.key === "Tab") {
        const dialog = document.querySelector(".detail-drawer");
        const elements = dialog?.querySelectorAll<HTMLElement>(
          'button, a[href], input, textarea, select, [tabindex="0"], summary',
        );
        if (!elements?.length) return;
        const first = elements[0];
        const last = elements[elements.length - 1];
        if (key.shiftKey && document.activeElement === first) {
          key.preventDefault();
          last.focus();
        } else if (!key.shiftKey && document.activeElement === last) {
          key.preventDefault();
          first.focus();
        }
      }
    };
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    document.addEventListener("keydown", keydown);
    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener("keydown", keydown);
      before?.focus();
    };
  }, [close]);
  useEffect(() => {
    const controller = new AbortController();
    api<LifeEvent>(`/events/${encodeURIComponent(id)}`, {
      signal: controller.signal,
    })
      .then(setEvent)
      .catch((cause) => {
        if (!controller.signal.aborted) setError(cause.message);
      });
    return () => controller.abort();
  }, [id]);
  return (
    <div className="drawer-overlay">
      <button
        className="drawer-scrim"
        aria-label="Close record"
        onClick={close}
      />
      <section
        className="detail-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="drawer-title"
      >
        <header className="drawer-header">
          <span>
            <FileText size={15} />
            RECORD
          </span>
          <button
            ref={closeButton}
            onClick={close}
            className="icon-button"
            aria-label="Close record"
          >
            <X size={20} />
          </button>
        </header>
        {error ? (
          <ErrorNotice message={error} />
        ) : !event ? (
          <div className="drawer-loading">
            <LoaderCircle className="spin" size={22} />
            Loading record…
          </div>
        ) : (
          <div className="drawer-content">
            <span className="event-source color-0">
              <span className="source-dot" />
              {event.source_label || event.source}
            </span>
            <h2 id="drawer-title">{event.title || "Untitled record"}</h2>
            <div className="drawer-date">
              <CalendarDays size={15} />
              {formatDate(event.occurred_at, event.time_precision)}
              {event.end_at &&
                ` – ${formatDate(event.end_at, event.time_precision)}`}
            </div>
            <div className="detail-tags">
              <span className="category-tag">
                {humanize(event.display_category || event.category || "record")}
              </span>
              {event.time_precision && (
                <span className="category-tag">
                  {humanize(event.time_precision)} precision
                </span>
              )}
            </div>
            <div className="event-body">
              {event.body ? (
                <ReadableData value={event.body} />
              ) : (
                "No text content."
              )}
            </div>
            <MediaGallery media={event.media} />
            <EnrichmentEvidence event={event} />
            <section className="evidence-section">
              <h3>
                <Layers3 size={15} />
                Evidence
              </h3>
              <p className="section-description">
                Source files for this record.
              </p>
              {event.provenance?.length ? (
                event.provenance.map((item, index) => (
                  <div
                    className="evidence-file"
                    key={`${item.file_id}-${index}`}
                  >
                    <div>
                      <FileText size={18} />
                      <span className="evidence-path">{item.path}</span>
                      <a
                        href={`/api/files/${encodeURIComponent(item.file_id)}/download`}
                        target="_blank"
                        rel="noreferrer"
                        aria-label={`Open original file ${item.path}`}
                        title="Open original file"
                      >
                        <ArrowUpRight size={17} />
                      </a>
                    </div>
                    {item.locator != null && (
                      <div className="evidence-locator">
                        <span>Location</span>
                        <ReadableData value={item.locator} />
                      </div>
                    )}
                    {item.sha256 && (
                      <details>
                        <summary>File fingerprint</summary>
                        <code>{item.sha256}</code>
                      </details>
                    )}
                  </div>
                ))
              ) : (
                <div className="notice">
                  <CircleAlert size={15} />
                  <span>
                    No original file links are recorded for this record.
                  </span>
                </div>
              )}
            </section>
            {event.metadata &&
              Object.keys(event.metadata).some(
                (key) => key !== "enrichment",
              ) && (
                <details className="metadata">
                  <summary>Record details</summary>
                  <ReadableData
                    value={Object.fromEntries(
                      Object.entries(event.metadata).filter(
                        ([key]) => key !== "enrichment",
                      ),
                    )}
                  />
                </details>
              )}
            <div className="record-id">Record {event.id}</div>
          </div>
        )}
      </section>
    </div>
  );
}

function SourceLibrary({
  overview,
  sources,
  imports,
  loading,
  error,
  refresh,
  browseSource,
}: {
  overview: Overview | null;
  sources: Source[];
  imports: ImportRun[];
  loading: boolean;
  error: string;
  refresh: () => void;
  browseSource: (id: string) => void;
}) {
  const sourceSort = useSortPreference(
    "sources", "name", sourceSortOptions.map(([field]) => field),
  );
  const importOptions = [["date", "Started"], ...sourceSortOptions] as const;
  const importSort = useSortPreference(
    "import-history", "date", importOptions.map(([field]) => field), "desc",
  );
  const [importOffset, setImportOffset] = useState(0);
  const [importPage, setImportPage] = useState<Page<ImportRun> | null>(null);
  const [importError, setImportError] = useState("");
  const [importLoading, setImportLoading] = useState(false);
  const importItems = importPage?.items ?? imports;
  const importPath = importSort[0] === "date" && importSort[1] === "desc" && !importOffset
    ? "/imports"
    : `/imports?sort=${importSort[0]}&direction=${importSort[1]}&offset=${importOffset}`;
  const importChoice: SortPreference = [
    importSort[0], importSort[1],
    field => { setImportOffset(0); importSort[2](field); },
    direction => { setImportOffset(0); importSort[3](direction); },
  ];
  useEffect(() => {
    const controller = new AbortController();
    setImportLoading(true);
    api<Page<ImportRun>>(importPath, { signal: controller.signal })
      .then(page => {
        if (!controller.signal.aborted) {
          setImportPage(page);
          setImportError("");
        }
      })
      .catch(cause => {
        if (!controller.signal.aborted) setImportError((cause as Error).message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setImportLoading(false);
      });
    return () => controller.abort();
  }, [importPath, imports]);
  return (
    <div className="page library-page">
      <div className="page-heading">
        <div>
          <h1>Sources</h1>
          <p>Original files and import status.</p>
        </div>
      </div>
      <CountFreshness stats={overview?.stats} />
      <div className="library-summary">
        <div>
          <Database size={19} />
          <span>
            <strong>{overview ? count(overview.sources_count) : "—"}</strong>{" "}
            sources
          </span>
        </div>
        <div>
          <FileText size={19} />
          <span>
            <strong>{overview ? count(overview.files_count) : "—"}</strong>{" "}
            original files
          </span>
        </div>
        <div>
          <HardDrive size={19} />
          <span>
            <strong>{overview ? bytes(overview.stored_bytes) : "—"}</strong>{" "}
            preserved
          </span>
        </div>
      </div>
      {error && <ErrorNotice message={error} retry={refresh} />}
      {(importItems.length > 0 || importOffset > 0 || importError.length > 0) && (
        <section className="imports-section">
          <div className="section-title">
            <h2>Import activity</h2>
            <SortControls label="Import activity" options={importOptions} value={importChoice} />
          </div>
          <p className="coverage-description">
            Browse order only. Execution order is shown in Imports.
          </p>
          {importError && <ErrorNotice message={importError} retry={refresh} />}
          <div className="import-list">
            {importLoading
              ? <p role="status">Loading import activity…</p>
              : !importError && importItems.map((item) => <ImportCard key={item.id} item={item} />)}
          </div>
          {!importError && <BrowsePages page={importPage} offset={importOffset} setOffset={setImportOffset} label="Imports" loading={importLoading} />}
        </section>
      )}
      <section>
        <div className="section-title">
          <h2>Sources</h2>
          <span>{count(sources.length)} connected</span>
        </div>
        <SortControls label="Sources" options={sourceSortOptions} value={sourceSort} />
        {loading ? (
          <div className="drawer-loading">
            <LoaderCircle className="spin" size={20} />
            Loading sources…
          </div>
        ) : !sources.length ? (
          <EmptyState icon={<FolderArchive size={28} />} title="No sources">
            Imported sources appear here.
          </EmptyState>
        ) : (
          <div className="source-grid">
            {sortItems(sources, sourceSort, (source, field) =>
              field === "name" ? source.name
                : field === "status" ? source.status
                : field === "records" ? source.event_count
                : field === "files" ? source.file_count : source.bytes,
            ).map((source) => (
              <article className="source-card" key={source.id}>
                <div className="source-card-heading">
                  <span className={`large-source-glyph color-${sources.indexOf(source) % 5}`}>
                    <Database size={21} />
                  </span>
                  <Status value={source.status || "unknown"} />
                </div>
                <h3>{source.name}</h3>
                <div className="source-card-count">
                  Records: <strong>{recordCount(source.event_count, source.stats)}</strong>
                  <span> · {count(source.file_count)} files</span>
                </div>
                <div className="source-date-range">
                  <CalendarDays size={13} />
                  {source.first_event || source.last_event
                    ? `${formatDate(source.first_event)} – ${formatDate(source.last_event)}`
                    : source.stats?.status === "current" ||
                        source.stats?.status === "ready"
                      ? "No dated records"
                      : "Dates pending"}
                </div>
                <CountFreshness stats={source.stats} />
                {source.notes &&
                  (typeof source.notes === "string"
                    ? source.notes
                    : source.notes.length > 0) && (
                    <div className="source-notes">
                      <ReadableData value={source.notes} />
                    </div>
                  )}
                <div className="source-card-footer">
                  <span>{bytes(source.bytes)} preserved</span>
                  <button onClick={() => browseSource(source.id)}>
                    Browse records
                    <ArrowRight size={14} />
                  </button>
                </div>
              </article>
            ))}
          </div>
        )}
      </section>
      <div className="coverage-note">
        <Layers3 size={20} />
        <div>
          <h3>Coverage</h3>
          <p>Missing records do not establish that nothing happened.</p>
        </div>
      </div>
    </div>
  );
}

function ImportCard({ item }: { item: ImportRun }) {
  const percentage = item.total_bytes
    ? Math.min(100, (item.completed_bytes / item.total_bytes) * 100)
    : item.total_files
      ? Math.min(100, (item.completed_files / item.total_files) * 100)
      : 0;
  return (
    <div className="import-card">
      <div className="import-top">
        <div>
          <FolderArchive size={17} />
          <strong>{item.source}</strong>
        </div>
        <Status value={item.status} />
      </div>
      <div
        className="progress-track"
        role="progressbar"
        aria-label={`${item.source} import`}
        aria-valuenow={Math.round(percentage)}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        <div style={{ width: `${percentage}%` }} />
      </div>
      <div className="import-details">
        <span>
          {count(item.completed_files)} / {count(item.total_files)} files
          <span className="hide-mobile">
            {" "}
            · {bytes(item.completed_bytes)} / {bytes(item.total_bytes)}
          </span>
        </span>
        <span>{count(item.events)} records</span>
      </div>
      {item.error && <p className="import-error">{item.error}</p>}
    </div>
  );
}

function BrowsePages({ page, offset, setOffset, label, loading }: {
  page: Page<unknown> | null;
  offset: number;
  setOffset: (offset: number) => void;
  label: string;
  loading: boolean;
}) {
  if (!page || (!offset && !hasMore(page))) return null;
  const size = page.limit || page.items.length || 40;
  return (
    <div className="pagination" aria-label={`${label} pages`}>
      <span>{page.items.length
        ? `${count(offset + 1)}–${count(offset + page.items.length)} shown`
        : `No ${label.toLowerCase()} on this page`}</span>
      <div>
        <button disabled={loading || !offset} onClick={() => setOffset(Math.max(0, offset - size))}>
          Previous {label.toLowerCase()}
        </button>
        <button disabled={loading || !hasMore(page)} onClick={() => setOffset(offset + size)}>
          Next {label.toLowerCase()}
        </button>
      </div>
    </div>
  );
}

function Assistant({ onChange }: { onChange: () => void }) {
  const [mode, setMode] = useState<"ask" | "change">("ask");
  const [prompt, setPrompt] = useState("");
  const [jobs, setJobs] = useState<Job[]>([]);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [undoing, setUndoing] = useState("");
  const [notice, setNotice] = useState("");
  const [selectedEvidence, setSelectedEvidence] = useState<string | null>(null);
  const jobOptions = [
    ["date", "Created"], ["status", "Status"], ["kind", "Kind"], ["prompt", "Question"],
  ] as const;
  const jobSort = useSortPreference(
    "assistant-jobs", "date", jobOptions.map(([field]) => field), "desc",
  );
  const [jobOffset, setJobOffset] = useState(0);
  const [jobPage, setJobPage] = useState<Page<Job> | null>(null);
  const [jobRefresh, setJobRefresh] = useState(0);
  const [jobLoadError, setJobLoadError] = useState("");
  const jobPath = jobSort[0] === "date" && jobSort[1] === "desc" && !jobOffset
    ? "/jobs"
    : `/jobs?sort=${jobSort[0]}&direction=${jobSort[1]}&offset=${jobOffset}`;
  const jobChoice: SortPreference = [
    jobSort[0], jobSort[1],
    field => { setJobOffset(0); jobSort[2](field); },
    direction => { setJobOffset(0); jobSort[3](direction); },
  ];
  const refreshJobs = () => setJobRefresh(value => value + 1);
  useEffect(() => {
    let live = true;
    const controller = new AbortController();
    setLoading(true);
    const load = async () => {
      try {
        const result = await api<Page<Job>>(jobPath, { signal: controller.signal });
        if (live) {
          setJobs(result.items);
          setJobPage(result);
          setJobLoadError("");
          setError("");
        }
      } catch (cause) {
        if (live) setJobLoadError((cause as Error).message);
      } finally {
        if (live) setLoading(false);
      }
    };
    void load();
    const timer = window.setInterval(() => {
      void load();
    }, 4000);
    return () => {
      live = false;
      controller.abort();
      window.clearInterval(timer);
    };
  }, [jobPath, jobRefresh]);
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!prompt.trim() || submitting) return;
    setSubmitting(true);
    setError("");
    setNotice("");
    try {
      await api("/assistant", {
        method: "POST",
        body: JSON.stringify({ prompt: prompt.trim(), mode }),
      });
      setPrompt("");
      await refreshJobs();
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setSubmitting(false);
    }
  };
  const undo = async (job: Job) => {
    setUndoing(job.id);
    setError("");
    try {
      await api(`/jobs/${encodeURIComponent(job.id)}/undo`, { method: "POST" });
      await refreshJobs();
      onChange();
      setNotice(
        "The dashboard change has been undone. Reload to see the restored version.",
      );
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setUndoing("");
    }
  };
  return (
    <div className="page assistant-page">
      <div className="page-heading">
        <div>
          <h1>Ask</h1>
          <p>Ask about records or change the app.</p>
        </div>
      </div>
      <SavedSummaries onOpenEvent={setSelectedEvidence} />
      <form className="prompt-composer" onSubmit={submit}>
        <div className="composer-top">
          <div className="mode-switch" role="group" aria-label="Assistant mode">
            <button
              className={mode === "ask" ? "selected" : ""}
              type="button"
              onClick={() => setMode("ask")}
            >
              <MessageSquareText size={14} />
              Ask
            </button>
            <button
              className={mode === "change" ? "selected" : ""}
              type="button"
              onClick={() => setMode("change")}
            >
              <SlidersHorizontal size={14} />
              Change app
            </button>
          </div>
          <span className="small-badge">CODEX</span>
        </div>
        <textarea
          aria-label={mode === "ask" ? "Ask" : "Describe a change"}
          placeholder={
            mode === "ask" ? "Ask about records…" : "Describe the app change…"
          }
          value={prompt}
          onChange={(event) => setPrompt(event.target.value)}
          rows={4}
          maxLength={8000}
        />
        <div className="composer-bottom">
          <span>
            <LockKeyhole size={12} />
            {mode === "ask"
              ? "Answers use your local archive as context."
              : "Dashboard changes are tested locally and can be undone."}
          </span>
          <button
            className="primary-button"
            disabled={submitting || !prompt.trim()}
            type="submit"
          >
            {submitting ? (
              <LoaderCircle className="spin" size={15} />
            ) : (
              <Send size={15} />
            )}
            {submitting ? "Starting…" : mode === "ask" ? "Send" : "Run change"}
          </button>
        </div>
      </form>
      {error && <ErrorNotice message={error} />}
      {notice && (
        <div className="notice success" role="status">
          <Check size={16} />
          {notice}
          <button onClick={() => window.location.reload()}>
            Reload dashboard
          </button>
        </div>
      )}
      <section className="jobs-section">
        <div className="section-title">
          <h2>Activity</h2>
          <SortControls label="Assistant activity" options={jobOptions} value={jobChoice} />
        </div>
        {loading ? (
          <div className="drawer-loading">
            <LoaderCircle className="spin" size={19} />
            Loading your activity…
          </div>
        ) : jobLoadError ? <ErrorNotice message={jobLoadError} retry={refreshJobs} /> : !jobs.length ? (
          <EmptyState
            icon={<MessageSquareText size={27} />}
            title={jobOffset ? "No activity on this page" : "No questions yet"}
          >
            Questions, answers, and app changes appear here.
          </EmptyState>
        ) : (
          <div className="job-list">
            {jobs.map((job) => (
              <article className="job-card" key={job.id}>
                <div className="job-top">
                  <span>
                    {job.kind === "change" ? (
                      <SlidersHorizontal size={14} />
                    ) : (
                      <MessageSquareText size={14} />
                    )}
                    {humanize(job.kind)}
                    <span className="job-date">
                      {formatDate(job.created_at)}
                    </span>
                  </span>
                  <Status value={job.status} />
                </div>
                <h3>{job.prompt}</h3>
                {job.result != null && (
                  <JobResult
                    result={job.result}
                    onEvidence={setSelectedEvidence}
                  />
                )}
                {job.error && <p className="job-error">{job.error}</p>}
                {job.can_undo && (
                  <div className="job-footer">
                    <button onClick={() => window.location.reload()}>
                      View dashboard
                      <ArrowUpRight size={14} />
                    </button>
                    <button
                      disabled={Boolean(undoing)}
                      onClick={() => {
                        void undo(job);
                      }}
                    >
                      {undoing === job.id ? (
                        <LoaderCircle className="spin" size={14} />
                      ) : (
                        <Undo2 size={14} />
                      )}
                      Undo this change
                    </button>
                  </div>
                )}
              </article>
            ))}
          </div>
        )}
        {!jobLoadError && <BrowsePages page={jobPage} offset={jobOffset} setOffset={setJobOffset} label="Activity" loading={loading} />}
      </section>
      <div className="assistant-disclosure">
        <CircleAlert size={14} />
        <span>
          Archive context is sent to the configured Codex service when you
          submit a request. Answers can be incomplete. Check the original
          evidence.
        </span>
      </div>
      {selectedEvidence && (
        <EventDrawer
          id={selectedEvidence}
          close={() => setSelectedEvidence(null)}
        />
      )}
    </div>
  );
}

function JobResult({
  result,
  onEvidence,
}: {
  result: unknown;
  onEvidence: (id: string) => void;
}) {
  if (typeof result === "string")
    return (
      <div className="job-result">
        <ReadableData value={result} />
      </div>
    );
  if (result && typeof result === "object") {
    const value = result as Record<string, unknown>;
    if ((value.kind === "period_summary" || value.kind === "trip_summary") && Array.isArray(value.sections) && value.coverage && value.cache)
      return <SummaryView result={result as SavedNarrative} onOpenEvent={onEvidence} />;
    if (typeof value.answer === "string") {
      const citations = Array.isArray(value.citations)
        ? value.citations.filter((id): id is string => typeof id === "string")
        : [];
      return (
        <div className="job-result">
          <CitedProse
            text={value.answer}
            citations={citations}
            onEvidence={onEvidence}
          />
          {citations.length > 0 && (
            <div className="job-citations">
              <span>ORIGINAL EVIDENCE</span>
              {citations.map((id, index) => (
                <button key={id} onClick={() => onEvidence(id)} title={id}>
                  <FileText size={12} />
                  Record {index + 1}
                  <ArrowUpRight size={11} />
                </button>
              ))}
            </div>
          )}
          <div className="answer-coverage">
            {typeof value.evidence_count === "number"
              ? `${count(value.evidence_count)} records supplied as context`
              : "Based on the evidence supplied for this question"}
            {typeof value.model === "string" ? ` · ${value.model}` : ""}
          </div>
        </div>
      );
    }
    if (typeof value.summary === "string")
      return (
        <div className="job-result">
          <div>{value.summary}</div>
          {Array.isArray(value.files) && (
            <div className="answer-coverage">
              {value.files.length} dashboard files changed. Reload to view the
              result.
            </div>
          )}
          <details className="result-details">
            <summary>Revision details</summary>
            <ReadableData value={value} />
          </details>
        </div>
      );
  }
  return (
    <div className="job-result">
      <ReadableData value={result} />
    </div>
  );
}
