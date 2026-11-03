import { useEffect, useState } from "react";
import {
  ArrowUpRight,
  CalendarDays,
  ChevronLeft,
  ChevronRight,
  CircleAlert,
  FileText,
  Layers3,
  LoaderCircle,
} from "lucide-react";
import {
  api,
  count,
  formatDate,
  humanize,
  type DailyRecord,
  type DailyRollup,
  type DayDetail,
  type DaysPage,
  type ProjectionStatus,
  type RollupPage,
} from "./api";
import MediaGallery from "./MediaGallery";
import MonthlySummary from "./MonthlySummary";
import ReadableData, { readablePreview } from "./ReadableData";

type DailyFilters = {
  q: string;
  source: string;
  category: string;
  from: string;
  to: string;
  undated: boolean;
};
function ErrorState({ error, retry }: { error: string; retry: () => void }) {
  return (
    <div className="notice error" role="alert">
      <CircleAlert size={16} />
      <span>{error}</span>
      <button onClick={retry}>Try again</button>
    </div>
  );
}
function RecordEvidence({
  event,
  onOpenEvent,
}: {
  event: DailyRecord;
  onOpenEvent: (id: string) => void;
}) {
  return (
    <article className="day-evidence-record">
      <button
        className="day-evidence-open"
        onClick={() => onOpenEvent(event.id)}
      >
        <span className="day-evidence-source">
          <span>{event.source_label || event.source}</span>
          <span>
            {humanize(
              event.display_category ||
                event.display_class ||
                event.category ||
                "record",
            )}
          </span>
        </span>
        <h4>{event.title || "Untitled record"}</h4>
        {event.body && <p>{readablePreview(event.body)}</p>}
        <span className="day-open-link">
          <FileText size={11} />
          Open record
          <ArrowUpRight size={11} />
        </span>
      </button>
      {typeof event.duplicate_count === "number" &&
        event.duplicate_count > 0 && (
          <p className="day-duplicate-note">
            {count(event.duplicate_count)} additional proven duplicate
            {event.duplicate_count === 1 ? "" : "s"} grouped with this
            representative
            {typeof event.source_record_count === "number"
              ? ` · ${count(event.source_record_count)} source records total`
              : ""}
            .
          </p>
        )}
      <MediaGallery media={event.media} compact />
      {event.body && (
        <details className="raw-source-text">
          <summary>Source text sample</summary>
          <div className="source-text-sample">
            <ReadableData
              value={
                event.body.length > 4000
                  ? readablePreview(event.body).slice(0, 4000)
                  : event.body
              }
            />
          </div>
          {event.body.length > 4000 && (
            <p>
              Preview shortened. Open the record for the full retained text.
            </p>
          )}
        </details>
      )}
    </article>
  );
}
function ProjectionNote({ projection }: { projection?: ProjectionStatus }) {
  if (!projection) return null;
  const watermark = projection.watermark_rowid;
  return (
    <div className="daily-projection-note">
      <Layers3 size={13} />
      <span>
        Daily index: {humanize(projection.status || "unknown")}
        {typeof watermark === "number"
          ? ` · index target ${count(watermark)}`
          : ""}
        {typeof projection.current_catalog_max_rowid === "number"
          ? ` · current catalog ${count(projection.current_catalog_max_rowid)}`
          : ""}
        {projection.rebuild_required
          ? " · rebuild required"
          : projection.caught_up_to_watermark === false
            ? " · processing its snapshot"
            : ""}
        .{" "}
        {typeof projection.append_backlog_rowid_span === "number" &&
        projection.append_backlog_rowid_span > 0
          ? "New catalog positions are waiting for the next index update. "
          : ""}
        Positions are not record counts.
      </span>
    </div>
  );
}
function RollupEvidence({
  rollup,
  onOpenEvent,
}: {
  rollup: DailyRollup;
  onOpenEvent: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [page, setPage] = useState<RollupPage | null>(null);
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setLoading(true);
    setError("");
    api<RollupPage>(
      `/rollups/${encodeURIComponent(rollup.id)}?limit=20&offset=${offset}`,
      { signal: controller.signal },
    )
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
  }, [open, rollup.id, offset, retry]);
  return (
    <details
      className="day-section raw-sample-section"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        {humanize(rollup.metric)}: inspect raw samples
        <span>
          {count(rollup.source_record_count ?? rollup.sample_count)} retained
          source records
        </span>
      </summary>
      <div className="day-section-content">
        <p className="daily-basis-note">
          {rollup.source}
          {rollup.unit ? ` · Unit: ${rollup.unit}` : ""}
          {rollup.device ? ` · Source device label: ${rollup.device}` : ""}.
          These are observed samples, not counts of completed activities.
        </p>
        {error ? (
          <ErrorState
            error={error}
            retry={() => setRetry((value) => value + 1)}
          />
        ) : loading ? (
          <div className="context-loading">
            <LoaderCircle size={16} className="spin" />
            Loading retained samples…
          </div>
        ) : (
          page?.items.map((event) => (
            <RecordEvidence
              key={event.id}
              event={event}
              onOpenEvent={onOpenEvent}
            />
          ))
        )}
        {!loading && page && (
          <div className="daily-pager">
            <span>{count(page.total)} retained source records</span>
            <div>
              <button
                disabled={offset === 0}
                onClick={() => setOffset((value) => Math.max(0, value - 20))}
              >
                Previous samples
              </button>
              <button
                disabled={offset + 20 >= page.total}
                onClick={() => setOffset((value) => value + 20)}
              >
                Next samples
              </button>
            </div>
          </div>
        )}
      </div>
    </details>
  );
}
function MetricSummary({ rollup }: { rollup: DailyRollup }) {
  const value =
    rollup.aggregation === "sum"
      ? rollup.sum
      : rollup.aggregation === "latest"
        ? rollup.latest
        : null;
  const number = (value: number | null) =>
    typeof value === "number"
      ? value.toLocaleString("en-US", { maximumFractionDigits: 3 })
      : "Not supplied";
  return (
    <article className="day-metric">
      <h4>{humanize(rollup.metric)}</h4>
      {typeof value === "number" ? (
        <strong>
          {number(value)}
          <span>{rollup.unit || ""}</span>
        </strong>
      ) : (
        <strong>
          {count(rollup.sample_count)}
          <span>sample occurrences</span>
        </strong>
      )}
      <p>
        {rollup.source}
        {rollup.device ? ` · ${rollup.device}` : ""}
        <br />
        Aggregation: {humanize(rollup.aggregation)} ·{" "}
        {count(rollup.numeric_count)} numeric / {count(rollup.sample_count)}{" "}
        sample occurrences{rollup.unit ? ` · ${rollup.unit}` : ""}
      </p>
      <details className="metric-details">
        <summary>Measurement details</summary>
        <dl>
          <div>
            <dt>Observed minimum</dt>
            <dd>{number(rollup.min)}</dd>
          </div>
          <div>
            <dt>Observed maximum</dt>
            <dd>{number(rollup.max)}</dd>
          </div>
          <div>
            <dt>Sample mean</dt>
            <dd>{number(rollup.sample_mean)}</dd>
          </div>
          <div>
            <dt>First sample</dt>
            <dd>{formatDate(rollup.first_at)}</dd>
          </div>
          <div>
            <dt>Last sample</dt>
            <dd>{formatDate(rollup.last_at)}</dd>
          </div>
        </dl>
        <p>
          Sample means describe retained observations, not a continuous
          measurement of the day.
        </p>
      </details>
    </article>
  );
}

export default function DailyWorkspace({
  filters,
  refreshKey,
  onOpenEvent,
}: {
  filters: DailyFilters;
  refreshKey: number;
  onOpenEvent: (id: string) => void;
}) {
  const [days, setDays] = useState<DaysPage | null>(null);
  const [day, setDay] = useState("");
  const [detail, setDetail] = useState<DayDetail | null>(null);
  const [daysLoading, setDaysLoading] = useState(true);
  const [loading, setLoading] = useState(false);
  const [daysError, setDaysError] = useState("");
  const [error, setError] = useState("");
  const [indexOffset, setIndexOffset] = useState(0);
  const [offset, setOffset] = useState(0);
  const [technical, setTechnical] = useState(false);
  const [retry, setRetry] = useState(0);
  const { q, source, category, from, to, undated } = filters;
  const key = [q, source, category, from, to, undated].join("\0");
  const [previous, setPrevious] = useState(key);
  if (previous !== key) {
    setPrevious(key);
    setIndexOffset(0);
    setOffset(0);
    setDay(undated ? "undated" : "");
  }
  useEffect(() => {
    const controller = new AbortController();
    setDaysLoading(true);
    setDaysError("");
    const params = new URLSearchParams({
      limit: "31",
      offset: String(indexOffset),
    });
    for (const [name, value] of Object.entries({
      q,
      source,
      category,
      from,
      to,
    }))
      if (value) params.set(name, value);
    if (undated) params.set("undated", "true");
    api<DaysPage>(`/days?${params}`, { signal: controller.signal })
      .then((result) => {
        if (controller.signal.aborted) return;
        setDays(result);
        setDay(
          (value) =>
            value ||
            (undated
              ? "undated"
              : result.items.find((item) => item.day !== "undated")?.day ||
                result.items[0]?.day ||
                ""),
        );
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setDaysError(cause.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setDaysLoading(false);
      });
    return () => controller.abort();
  }, [q, source, category, from, to, undated, indexOffset, refreshKey, retry]);
  useEffect(() => {
    if (!day) {
      setDetail(null);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError("");
    setDetail(null);
    const params = new URLSearchParams({
      limit: "20",
      offset: String(offset),
      include_technical: String(technical),
    });
    for (const [name, value] of Object.entries({ q, source, category }))
      if (value) params.set(name, value);
    api<DayDetail>(`/days/${encodeURIComponent(day)}?${params}`, {
      signal: controller.signal,
    })
      .then((result) => {
        if (!controller.signal.aborted) setDetail(result);
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setError(cause.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [day, q, source, category, offset, technical, refreshKey, retry]);
  const selectDay = (value: string) => {
    setDay(value);
    setOffset(0);
  };
  const moveDay = (amount: number) => {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return;
    const next = new Date(`${day}T12:00:00Z`);
    next.setUTCDate(next.getUTCDate() + amount);
    selectDay(next.toISOString().slice(0, 10));
  };
  return (
    <section
      className="daily-workspace"
      aria-label="Daily summaries and source evidence"
    >
      <header className="daily-heading">
        <div>
          <h2>
            <CalendarDays size={19} />
            Days
          </h2>
          <p>Records and measurement streams by source date.</p>
        </div>
      </header>
      {daysError && (
        <ErrorState
          error={daysError}
          retry={() => setRetry((value) => value + 1)}
        />
      )}
      <MonthlySummary day={day} sourceId={source} onOpenEvent={onOpenEvent} />
      <div className="daily-layout">
        <aside className="daily-date-rail">
          <h3>OBSERVED DAYS</h3>
          {daysLoading ? (
            <div className="daily-index-loading">
              <LoaderCircle size={15} className="spin" />
              Loading days…
            </div>
          ) : (
            <div className="daily-date-list">
              {days?.items
                .filter((item) => item.day !== "undated")
                .map((item) => (
                  <button
                    key={item.day}
                    className={day === item.day ? "selected" : ""}
                    onClick={() => selectDay(item.day)}
                  >
                    <span>{formatDate(item.date || item.day)}</span>
                    <span>{count(item.observed_records)} records</span>
                  </button>
                ))}
            </div>
          )}
          <button
            className={`daily-undated ${day === "undated" ? "selected" : ""}`}
            onClick={() => selectDay("undated")}
          >
            Undated records
            <span>
              Open
              <ArrowUpRight size={11} />
            </span>
          </button>
          {days && (
            <div className="day-index-pagination">
              <button
                aria-label="Newer observed days"
                disabled={indexOffset === 0}
                onClick={() =>
                  setIndexOffset((value) => Math.max(0, value - 31))
                }
              >
                <ChevronLeft size={14} />
              </button>
              <span>{count(days.total)} day buckets</span>
              <button
                aria-label="Older observed days"
                disabled={indexOffset + 31 >= days.total}
                onClick={() => setIndexOffset((value) => value + 31)}
              >
                <ChevronRight size={14} />
              </button>
            </div>
          )}
        </aside>
        <div className="daily-content">
          <div className="day-navigation">
            <div>
              <button
                className="icon-button"
                disabled={!day || day === "undated"}
                aria-label="Previous calendar day"
                onClick={() => moveDay(-1)}
              >
                <ChevronLeft size={17} />
              </button>
              <h3>
                {day === "undated"
                  ? "Undated records"
                  : day
                    ? formatDate(day)
                    : "Choose a day"}
              </h3>
              <button
                className="icon-button"
                disabled={!day || day === "undated"}
                aria-label="Next calendar day"
                onClick={() => moveDay(1)}
              >
                <ChevronRight size={17} />
              </button>
            </div>
            <input
              aria-label="Choose calendar day"
              type="date"
              value={day === "undated" ? "" : day}
              onChange={(event) => selectDay(event.target.value)}
            />
          </div>
          <p className="daily-basis-note">
            {day === "undated"
              ? "Records without a specific trustworthy day stay here. Their original dates and precision remain in the evidence."
              : "Day buckets follow the date basis supplied by each source. A day with no records is a gap in the archive, not evidence of no activity."}
          </p>
          {loading ? (
            <div className="context-loading" role="status">
              <LoaderCircle size={18} className="spin" />
              Loading the day and its evidence…
            </div>
          ) : error ? (
            <ErrorState
              error={error}
              retry={() => setRetry((value) => value + 1)}
            />
          ) : detail ? (
            <>
              <div className="daily-stat-line">
                <span>
                  <strong>{count(detail.summary.observed_records)}</strong>{" "}
                  observed source records
                </span>
                <span>
                  <strong>{count(detail.summary.substantive_records)}</strong>{" "}
                  substantive records
                </span>
                <span>
                  <strong>{count(detail.summary.raw_samples)}</strong> raw
                  samples
                </span>
                <span>
                  <strong>{count(detail.summary.source_count)}</strong> sources
                </span>
              </div>
              <div className="daily-summary">
                <h4>
                  <Layers3 size={14} />
                  Day summary
                </h4>
                <p>
                  {count(detail.summary.display_groups)} readable groups and{" "}
                  {count(detail.summary.measurement_groups)} measurement streams
                  are indexed for this selection. Counts refer to source
                  records, not completed activities.
                </p>
                {detail.summary.date_bases.length > 0 && (
                  <ul>
                    {detail.summary.date_bases.map((item) => (
                      <li key={item.basis}>
                        {humanize(item.basis)}: {count(item.count)} records
                      </li>
                    ))}
                  </ul>
                )}
              </div>
              {detail.rollups.length > 0 && (
                <>
                  <h4 className="daily-subheading">Measurement streams</h4>
                  {detail.rollups_truncated && (
                    <p className="daily-basis-note">
                      {count(detail.rollups.length)} of{" "}
                      {count(detail.rollups_total)} streams shown. Narrow the
                      source or type filter for more detail.
                    </p>
                  )}
                  <div className="day-metric-grid">
                    {detail.rollups.map((rollup) => (
                      <MetricSummary key={rollup.id} rollup={rollup} />
                    ))}
                  </div>
                  <div className="daily-raw-streams">
                    {detail.rollups.map((rollup) => (
                      <RollupEvidence
                        key={rollup.id}
                        rollup={rollup}
                        onOpenEvent={onOpenEvent}
                      />
                    ))}
                  </div>
                </>
              )}
              <label className="daily-technical-toggle">
                <input
                  type="checkbox"
                  checked={technical}
                  onChange={(event) => {
                    setTechnical(event.target.checked);
                    setOffset(0);
                  }}
                />
                Include technical traces
                <span>
                  {count(detail.summary.technical_records)} observed technical
                  records
                </span>
              </label>
              <details className="day-section" open>
                <summary>
                  Readable source evidence
                  <span>{count(detail.total)} representative groups</span>
                </summary>
                <div className="day-section-content">
                  {detail.items.length ? (
                    detail.items.map((event) => (
                      <RecordEvidence
                        key={event.id}
                        event={event}
                        onOpenEvent={onOpenEvent}
                      />
                    ))
                  ) : (
                    <p className="daily-empty-note">
                      No readable groups in this selection. Measurement streams
                      and retained samples may still be available above.
                    </p>
                  )}
                </div>
              </details>
              {detail.total > 20 && (
                <div className="daily-pager">
                  <span>
                    {count(offset + 1)}–
                    {count(Math.min(offset + 20, detail.total))} of{" "}
                    {count(detail.total)} readable groups
                  </span>
                  <div>
                    <button
                      disabled={offset === 0}
                      onClick={() =>
                        setOffset((value) => Math.max(0, value - 20))
                      }
                    >
                      Previous evidence
                    </button>
                    <button
                      disabled={offset + 20 >= detail.total}
                      onClick={() => setOffset((value) => value + 20)}
                    >
                      Next evidence
                    </button>
                  </div>
                </div>
              )}
            </>
          ) : !daysLoading && !daysError ? (
            <p className="daily-empty-note">
              No indexed days in this selection. Undated records and index
              status are available here.
            </p>
          ) : null}
          {q && days?.q_scope && (
            <p className="daily-basis-note">
              Search selects matching days. Counts cover each full day for the
              selected source and type.
            </p>
          )}
          <ProjectionNote projection={detail?.projection || days?.projection} />
        </div>
      </div>
    </section>
  );
}
