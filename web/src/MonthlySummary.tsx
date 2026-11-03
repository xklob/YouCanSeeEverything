import { useEffect, useRef, useState } from "react";
import {
  CircleAlert,
  LoaderCircle,
  Sparkles,
} from "lucide-react";
import {
  api,
  humanize,
  type Job,
  type PeriodSummary,
  type SummaryCache,
} from "./api";
import SummaryView from "./SummaryView";

export default function MonthlySummary({
  day,
  sourceId,
  onOpenEvent,
}: {
  day: string;
  sourceId: string;
  onOpenEvent: (id: string) => void;
}) {
  const [month, setMonth] = useState(
    /^\d{4}-\d{2}/.test(day) ? day.slice(0, 7) : "",
  );
  const [model, setModel] = useState("gpt-6.1-sol");
  const [focus, setFocus] = useState("");
  const [focusQuery, setFocusQuery] = useState("");
  const activeGeneration = useRef(false);
  const lookupEpoch = useRef(0);
  const [cache, setCache] = useState<SummaryCache | null>(null);
  const [result, setResult] = useState<PeriodSummary | null>(null);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    if (activeGeneration.current) return;
    if (/^\d{4}-\d{2}/.test(day)) setMonth(day.slice(0, 7));
  }, [day]);
  useEffect(() => {
    if (activeGeneration.current) return;
    const epoch = ++lookupEpoch.current;
    setCache(null);
    setResult(null);
    setJob(null);
    setError("");
    if (!month) return;
    const controller = new AbortController();
    setLoading(true);
    const params = new URLSearchParams({ period: month, model });
    if (sourceId) params.set("source", sourceId);
    if (focusQuery) params.set("focus", focusQuery);
    api<SummaryCache>(`/summaries?${params}`, { signal: controller.signal })
      .then((value) => {
        if (
          !controller.signal.aborted &&
          epoch === lookupEpoch.current &&
          !activeGeneration.current
        ) {
          setCache(value);
          setResult(value.result);
        }
      })
      .catch((cause) => {
        if (
          !controller.signal.aborted &&
          epoch === lookupEpoch.current &&
          !activeGeneration.current
        )
          setError(cause.message);
      })
      .finally(() => {
        if (!controller.signal.aborted && epoch === lookupEpoch.current)
          setLoading(false);
      });
    return () => controller.abort();
  }, [month, sourceId, model, focusQuery, retry]);
  const pending = Boolean(
    job && ["queued", "pending", "running"].includes(job.status),
  );
  useEffect(() => {
    if (!job || !["queued", "pending", "running"].includes(job.status)) return;
    const controller = new AbortController();
    const poll = async () => {
      try {
        const next = await api<Job>(`/jobs/${encodeURIComponent(job.id)}`, {
          signal: controller.signal,
        });
        if (controller.signal.aborted) return;
        setJob(next);
        if (!["queued", "pending", "running"].includes(next.status))
          activeGeneration.current = false;
        if (
          next.status === "completed" &&
          next.result &&
          typeof next.result === "object" &&
          (next.result as PeriodSummary).kind === "period_summary"
        ) {
          setCache(null);
          setResult(next.result as PeriodSummary);
        }
        if (next.error) setError(next.error);
      } catch (cause) {
        if (!controller.signal.aborted) setError((cause as Error).message);
      }
    };
    void poll();
    const timer = window.setInterval(() => {
      void poll();
    }, 2500);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [job?.id, job?.status]);
  const generate = async () => {
    if (!month || pending || submitting) return;
    const normalizedFocus = focus.trim();
    activeGeneration.current = true;
    lookupEpoch.current += 1;
    setLoading(false);
    setFocusQuery(normalizedFocus);
    setSubmitting(true);
    setError("");
    try {
      const created = await api<{ id: string; status: string }>("/assistant", {
        method: "POST",
        body: JSON.stringify({
          mode: "summarize",
          period: month,
          source_ids: sourceId ? [sourceId] : [],
          prompt: normalizedFocus,
          model,
        }),
      });
      setJob({
        id: created.id,
        status: created.status,
        kind: "summarize",
        prompt: normalizedFocus,
        created_at: new Date().toISOString(),
      });
    } catch (cause) {
      activeGeneration.current = false;
      setError((cause as Error).message);
    } finally {
      setSubmitting(false);
    }
  };
  return (
    <details className="monthly-summary-panel">
      <summary>
        <span>
          <Sparkles size={14} />
          Monthly summary
        </span>
        <span>
          {result
            ? "Available"
            : pending
              ? "In progress"
              : "Generate on request"}
        </span>
      </summary>
      <div className="monthly-summary-content">
        <p>
          Generating sends selected excerpts to Codex. Browsing does not run a
          model.
        </p>
        <div className="monthly-controls">
          <label>
            Month
            <input
              type="month"
              aria-label="Summary month"
              value={month}
              onChange={(event) => setMonth(event.target.value)}
              disabled={pending || submitting}
            />
          </label>
          <label>
            Model
            <select
              aria-label="Summary model"
              value={model}
              onChange={(event) => setModel(event.target.value)}
              disabled={pending || submitting}
            >
              <option value="gpt-6.1-sol">Sol · high</option>
              <option value="gpt-6-astra">Astra · xhigh</option>
            </select>
          </label>
          <span>{sourceId ? "Selected source filter" : "All sources"}</span>
        </div>
        <label className="summary-focus">
          Optional focus
          <input
            aria-label="Summary focus"
            value={focus}
            onChange={(event) => setFocus(event.target.value)}
            onBlur={() => setFocusQuery(focus.trim())}
            placeholder="Optional topic…"
            maxLength={4000}
            disabled={pending || submitting}
          />
        </label>
        <div className="summary-generate-row">
          <span>
            {loading
              ? "Checking saved summary…"
              : pending
                ? `Codex ${humanize(job!.status).toLowerCase()}…`
                : result
                  ? `Saved summary${cache?.status === "stale" || result.cache.status === "stale" ? " · stale for the current catalog" : ""}`
                  : cache?.status === "missing"
                    ? "No saved summary for this selection"
                    : "Choose a month to check saved summaries"}
          </span>
          <button
            onClick={() => {
              void generate();
            }}
            disabled={!month || pending || submitting}
          >
            {pending || submitting ? (
              <LoaderCircle size={13} className="spin" />
            ) : (
              <Sparkles size={13} />
            )}
            {pending || submitting ? "Working…" : "Generate summary"}
          </button>
        </div>
        {error && (
          <div className="notice error" role="alert">
            <CircleAlert size={14} />
            <span>{error}</span>
            <button onClick={() => setRetry((value) => value + 1)}>
              Recheck saved summary
            </button>
          </div>
        )}
        <SummaryView result={result} coverage={result?.coverage || cache?.current_coverage} onOpenEvent={onOpenEvent} />
      </div>
    </details>
  );
}
