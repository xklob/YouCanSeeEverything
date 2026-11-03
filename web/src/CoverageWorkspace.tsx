import { useEffect, useState } from "react";
import {
  ArrowUpRight,
  CircleAlert,
  Database,
  Files,
  FolderSync,
  Layers3,
  LoaderCircle,
  RefreshCw,
  ShieldCheck,
} from "lucide-react";
import {
  api,
  bytes,
  count,
  recordCount,
  formatDate,
  humanize,
  type CoverageReport,
} from "./api";
import ReadableData from "./ReadableData";
import {
  SortControls, sortItems, sourceSortOptions, useSortPreference,
} from "./sorting";

const measured = (value: number | null | undefined) =>
  typeof value === "number" ? count(value) : "Not counted";
function CoverageProgress({
  label,
  completed,
  total,
  fraction,
  asBytes = false,
}: {
  label: string;
  completed: number;
  total: number;
  fraction: number | null;
  asBytes?: boolean;
}) {
  const percent =
    typeof fraction === "number" && Number.isFinite(fraction)
      ? Math.max(0, Math.min(100, fraction * 100))
      : null;
  const value = (number: number) => (asBytes ? bytes(number) : count(number));
  return (
    <div className="coverage-progress">
      <div>
        <span>{label}</span>
        <span>
          {value(completed)} / {value(total)}
          {percent !== null ? ` · ${percent.toFixed(1)}%` : ""}
        </span>
      </div>
      <div
        className="coverage-progress-track"
        role="progressbar"
        aria-label={label}
        {...(percent === null
          ? {}
          : {
              "aria-valuenow": Math.round(percent),
              "aria-valuemin": 0,
              "aria-valuemax": 100,
            })}
      >
        <span style={{ width: `${percent ?? 0}%` }} />
      </div>
    </div>
  );
}

export default function CoverageWorkspace({
  refreshKey,
  onBrowseSource,
}: {
  refreshKey: number;
  onBrowseSource: (id: string) => void;
}) {
  const [report, setReport] = useState<CoverageReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const sourceSort = useSortPreference(
    "coverage-sources", "name", sourceSortOptions.map(([field]) => field),
  );
  const dailyWatermark =
    report?.indexing.daily.watermark_rowid ?? report?.indexing.daily.watermark;
  useEffect(() => {
    const controller = new AbortController();
    let inFlight = false;
    let timer: number | undefined;
    const load = async () => {
      if (controller.signal.aborted || inFlight) return;
      inFlight = true;
      try {
        const data = await api<CoverageReport>("/coverage", {
          signal: controller.signal,
        });
        if (!controller.signal.aborted) {
          setReport(data);
          setError("");
        }
      } catch (cause) {
        if (!controller.signal.aborted) setError((cause as Error).message);
      } finally {
        inFlight = false;
        if (!controller.signal.aborted) setLoading(false);
        if (!controller.signal.aborted)
          timer = window.setTimeout(() => {
            void load();
          }, 30000);
      }
    };
    void load();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [refreshKey, retry]);
  return (
    <div className="page coverage-page">
      <div className="page-heading">
        <div>
          <h1>Imports</h1>
          <p>File, extraction, and index status.</p>
        </div>
        <button
          className="coverage-refresh"
          onClick={() => setRetry((value) => value + 1)}
        >
          <RefreshCw size={14} />
          Refresh
        </button>
      </div>
      {error && (
        <div className="notice error" role="alert">
          <CircleAlert size={16} />
          <span>{error}</span>
          <button onClick={() => setRetry((value) => value + 1)}>
            Try again
          </button>
        </div>
      )}
      {loading && !report ? (
        <div className="drawer-loading" role="status">
          <LoaderCircle size={20} className="spin" />
          Loading import and coverage information…
        </div>
      ) : report ? (
        <>
          <div className="coverage-scope-note">
            <Layers3 size={17} />
            <p>
              Observed date ranges do not establish continuous coverage or
              inactivity in gaps.
            </p>
          </div>
          <div className="coverage-overview">
            <div>
              <Files size={18} />
              <strong>{measured(report.totals.files)}</strong>
              <span>File inventory records</span>
            </div>
            <div>
              <Database size={18} />
              <strong>{bytes(report.totals.bytes)}</strong>
              <span>File inventory bytes</span>
            </div>
            <div>
              <Layers3 size={18} />
              <strong>{recordCount(report.totals.events, {status: report.indexing.coverage_stats.status, counted_events: report.totals.counted_events})}</strong>
              <span>Observed source records</span>
            </div>
          </div>
          <div className="coverage-counts">
            <span>
              <strong>{measured(report.totals.parsed_files)}</strong> parsed
              files
            </span>
            <span>
              <strong>{measured(report.totals.metadata_only_files)}</strong>{" "}
              metadata-only files
            </span>
            <span>
              <strong>{measured(report.totals.retained_only_files)}</strong>{" "}
              retained-only files
            </span>
            <span>
              <strong>{measured(report.totals.review_files)}</strong> files
              needing review
            </span>
          </div>
          <section className="coverage-section">
            <div className="section-title">
              <h2>Import progress</h2>
              <span>Original input inventory</span>
            </div>
            <p className="coverage-description">
              Progress measures original input files and bytes. Retained
              inventory includes expanded files.
            </p>
            {report.active_imports.length ? (
              <div className="coverage-imports">
                {report.active_imports.map((item) => (
                  <article key={item.id} className="coverage-import">
                    <header>
                      <h3>
                        <FolderSync size={15} />
                        {item.source}
                      </h3>
                      <span className="coverage-state">
                        {humanize(item.status)}
                      </span>
                    </header>
                    <CoverageProgress
                      label={`${item.source}: input files`}
                      {...item.files}
                    />
                    <CoverageProgress
                      label={`${item.source}: input bytes`}
                      {...item.bytes}
                      asBytes
                    />
                    <p className="coverage-import-events">
                      {count(item.events_reported)} records reported by this
                      import
                    </p>
                    {item.source_cleanup && <p className="coverage-description">
                      Processed inputs: {count(item.source_cleanup.removed)} export copies removed after verification;{' '}
                      {count(item.source_cleanup.retained)} kept for review.
                    </p>}
                    {item.activity && (
                      <div className="coverage-current" aria-label={`${item.source}: current input`}>
                        <strong>{item.activity.label}</strong>
                        {item.activity.filename && <span className="coverage-current-file">{item.activity.filename}</span>}
                        {item.activity.progress && <CoverageProgress
                          label={`${item.source}: current check`}
                          {...item.activity.progress}
                          asBytes
                        />}
                        {item.activity.retained_entries != null && <span>
                          {count(item.activity.retained_entries)} entries verified from this input
                        </span>}
                        {item.activity.seconds_since_update != null && <span className={item.activity.seconds_since_update > 120 ? 'coverage-stale' : ''}>
                          {item.activity.seconds_since_update < 60
                            ? 'Updated just now'
                            : `Last update ${Math.floor(item.activity.seconds_since_update / 60)} min ago`}
                        </span>}
                      </div>
                    )}
                    {item.error && (
                      <p className="coverage-error">
                        {typeof item.error === "string"
                          ? item.error
                          : item.error.message}
                      </p>
                    )}
                  </article>
                ))}
              </div>
            ) : (
              <div className="coverage-quiet">
                <FolderSync size={17} />
                No active imports reported.
              </div>
            )}
          </section>
          <section className="coverage-section">
            <div className="section-title">
              <h2>Source coverage</h2>
              <span>{count(report.sources.length)} sources</span>
            </div>
            <SortControls label="Source coverage" options={sourceSortOptions} value={sourceSort} />
            <div className="coverage-sources">
              {sortItems(report.sources, sourceSort, (source, field) =>
                field === "name" ? source.name
                  : field === "status" ? source.status
                  : field === "records" ? source.records.total
                  : field === "files" ? source.files.total : source.files.bytes,
              ).map((source) => (
                <article className="coverage-source" key={source.id}>
                  <header>
                    <div>
                      <h3>{source.name}</h3>
                      <span>{humanize(source.status)}</span>
                    </div>
                    <button onClick={() => onBrowseSource(source.id)}>
                      Browse records
                      <ArrowUpRight size={13} />
                    </button>
                  </header>
                  <div className="source-coverage-stats">
                    <span>
                      <strong>{count(source.files.total)}</strong> files
                    </span>
                    <span>
                      Records: <strong>{recordCount(source.records.total, source.records)}</strong>
                    </span>
                    <span>
                      <strong>{measured(source.records.undated)}</strong>{" "}
                      undated
                    </span>
                    <span
                      className={`record-count-state ${source.records.status === "current" ? "current" : ""}`}
                    >
                      Counts {humanize(source.records.status).toLowerCase()}
                    </span>
                  </div>
                  <p className="observed-range">
                    Observed dates:{" "}
                    {source.records.first_observed
                      ? `${formatDate(source.records.first_observed)} – ${formatDate(source.records.last_observed)}`
                      : "No measured date range supplied"}
                  </p>
                  <p className="coverage-date-precision">
                    Source dates may be approximate. Original records retain
                    their date precision.
                  </p>
                  <div className="source-extraction-counts">
                    <span>{count(source.files.parsed)} parsed</span>
                    <span>{count(source.files.partial)} partial</span>
                    <span>
                      {count(source.files.metadata_only)} metadata-only
                    </span>
                    <span>
                      {count(source.files.retained_only)} retained-only
                    </span>
                    <span>{count(source.files.needs_review)} review</span>
                    <span>{count(source.files.pending)} pending</span>
                  </div>
                  <details className="coverage-source-details">
                    <summary>Extraction and count details</summary>
                    <p>
                      Retention preserves source files. Metadata-only and
                      retained-only files do not establish that their full
                      content has been extracted.
                    </p>
                    <dl>
                      <div>
                        <dt>Empty files</dt>
                        <dd>{count(source.files.empty)}</dd>
                      </div>
                      <div>
                        <dt>Excluded files</dt>
                        <dd>{count(source.files.excluded)}</dd>
                      </div>
                      <div>
                        <dt>Pending extraction</dt>
                        <dd>{count(source.extraction.pending_files)}</dd>
                      </div>
                      <div>
                        <dt>Extraction review</dt>
                        <dd>{count(source.extraction.review_files)}</dd>
                      </div>
                      <div>
                        <dt>Count measurement</dt>
                        <dd>
                          {source.records.measured_at
                            ? formatDate(source.records.measured_at)
                            : "Not supplied"}
                        </dd>
                      </div>
                    </dl>
                    {Object.keys(source.files.by_parse_status).length > 0 && (
                      <div className="coverage-file-states">
                        <h4>File states</h4>
                        <ReadableData value={source.files.by_parse_status} />
                      </div>
                    )}
                    {source.notes?.length > 0 && (
                      <ul>
                        {source.notes.map((note, index) => (
                          <li key={index}>
                            <ReadableData value={note} />
                          </li>
                        ))}
                      </ul>
                    )}
                  </details>
                </article>
              ))}
            </div>
          </section>
          {report.import_issues && report.import_issues.length > 0 && (
            <section className="coverage-section">
              <div className="section-title">
                <h2>Import issues</h2>
                <span>Latest reported issue per source</span>
              </div>
              {report.import_issues.map((item) => (
                <div className="coverage-import-issue" key={item.id}>
                  <h3>
                    {item.source}
                    <span>{humanize(item.status)}</span>
                  </h3>
                  <ReadableData value={item.error} />
                </div>
              ))}
            </section>
          )}
          <div className="coverage-bottom-grid">
            <section className="coverage-section">
              <div className="section-title">
                <h2>Queue</h2>
                <span>Source processing order</span>
              </div>
              {report.queue.length ? (
                <ol className="coverage-queue">
                  {report.queue.map((item) => (
                    <li key={item.source_id}>
                      <span>{item.position}</span>
                      <div>
                        <strong>{item.source}</strong>
                        <span>
                          {humanize(item.status)} · {bytes(item.snapshot_bytes)}{" "}
                          input snapshot
                        </span>
                        {item.error && (
                          <p className="coverage-error">
                            {typeof item.error === "string"
                              ? item.error
                              : item.error.message}
                          </p>
                        )}
                      </div>
                    </li>
                  ))}
                </ol>
              ) : (
                <p className="coverage-description">
                  No queued sources reported.
                </p>
              )}
            </section>
            <section className="coverage-section">
              <div className="section-title">
                <h2>Index & backup</h2>
                <span>Current data versus derived views</span>
              </div>
              <div className="coverage-index">
                <h3>
                  <Layers3 size={14} />
                  Daily view
                </h3>
                <span>
                  {report.indexing.daily.rebuild_required
                    ? "Update required"
                    : humanize(report.indexing.daily.status || "not supplied")}
                </span>
                <dl>
                  <div>
                    <dt>Daily index target</dt>
                    <dd>
                      {typeof dailyWatermark === "number"
                        ? count(dailyWatermark)
                        : "Not supplied"}
                    </dd>
                  </div>
                  <div>
                    <dt>Current catalog position</dt>
                    <dd>{count(report.indexing.catalog_max_event_rowid)}</dd>
                  </div>
                  <div>
                    <dt>Coverage count target</dt>
                    <dd>
                      {measured(
                        report.indexing.coverage_stats.catalog_max_event_rowid,
                      )}
                    </dd>
                  </div>
                  <div>
                    <dt>Coverage counts</dt>
                    <dd>{humanize(report.indexing.coverage_stats.status)}</dd>
                  </div>
                </dl>
                {report.indexing.daily.rebuild_required && (
                  <p>The daily view needs an update after record changes. Browse Records for the current archive.</p>
                )}
                <p>Positions are not record counts.</p>
              </div>
              <div className="coverage-backup">
                <ShieldCheck size={17} />
                <div>
                  <h3>Backup status</h3>
                  <p>
                    {report.backup.status === "not_supplied"
                      ? "Backup status was not supplied. Retained files alone do not verify a separate backup."
                      : humanize(report.backup.status)}
                  </p>
                  {typeof report.backup.last_verified_at === "string" && (
                    <p>
                      Last verified {formatDate(report.backup.last_verified_at)}
                    </p>
                  )}
                  {report.backup.status !== "not_supplied" && (
                    <>
                      <dl className="backup-facts">
                        <div>
                          <dt>Catalog cut</dt>
                          <dd>
                            {typeof report.backup.catalog_cut_at === "string"
                              ? formatDate(report.backup.catalog_cut_at)
                              : "Unknown"}
                          </dd>
                        </div>
                        <div>
                          <dt>Catalog records</dt>
                          <dd>
                            {typeof report.backup.catalog_records === "number"
                              ? count(report.backup.catalog_records)
                              : "Unknown"}
                          </dd>
                        </div>
                        <div>
                          <dt>Objects verified</dt>
                          <dd>
                            {typeof report.backup.objects_verified === "number"
                              ? count(report.backup.objects_verified)
                              : "Unknown"}
                          </dd>
                        </div>
                        <div>
                          <dt>Object bytes verified</dt>
                          <dd>
                            {typeof report.backup.object_bytes_verified ===
                            "number"
                              ? bytes(report.backup.object_bytes_verified)
                              : "Unknown"}
                          </dd>
                        </div>
                        {(
                          [
                            "restore_verified",
                            "independent_copy",
                            "remote_verified",
                          ] as const
                        ).map((field) => (
                          <div key={field}>
                            <dt>{humanize(field)}</dt>
                            <dd>
                              {report.backup[field] === true
                                ? "Verified"
                                : report.backup[field] === false
                                  ? "Not verified"
                                  : "Unknown"}
                            </dd>
                          </div>
                        ))}
                      </dl>
                      <p>Snapshot verification excludes later imports.</p>
                    </>
                  )}
                  {typeof report.backup.deferred_reason === "string" && (
                    <p>{report.backup.deferred_reason}</p>
                  )}
                </div>
              </div>
            </section>
          </div>
          <p className="coverage-asof">
            Coverage snapshot {formatDate(report.generated_at)}. Refreshes while
            this view is open.
          </p>
        </>
      ) : !error ? (
        <div className="coverage-quiet">No coverage report is available.</div>
      ) : null}
    </div>
  );
}
