import { useEffect, useState } from "react";
import {
  ArrowRight,
  ChevronLeft,
  ChevronRight,
  Map as MapIcon,
  Route,
} from "lucide-react";
import { api, count, formatDate } from "./api";
import { readablePreview } from "./ReadableData";
import type { TripConnection, TripStop } from "./maps/types";
import "./TripsWorkspace.css";
import TripSummary from "./TripSummary";

type EvidenceStop = TripStop & { provenance_total?: number };
type Journey = {
  id: string;
  source_id: string;
  source: string;
  departure_at: string;
  arrival_at: string;
  legs: number;
  distance_km: number;
  max_gap_hours: number;
  long_distance: boolean;
  represented_stops: number;
  first_stop: EvidenceStop;
  last_stop: EvidenceStop;
  summary_complete: boolean;
};
type JourneysPage = {
  items: Journey[];
  total: number;
  limit: number;
  offset: number;
  coverage_complete: boolean;
  sources: { id: string; name: string }[];
};
type JourneyDetail = {
  journey: Journey;
  items: TripConnection[];
  total: number;
  limit: number;
  offset: number;
  truncated: boolean;
  complete: boolean;
  coverage_complete: boolean;
};
export type TripMapRequest = {
  from: string;
  to: string;
  sourceId: string;
  tripId: string;
};
type Props = {
  refreshKey: number;
  onOpenEvent: (id: string) => void;
  onShowMap: (request: TripMapRequest) => void;
};
const PAGE_SIZE = 20;
const LEG_PAGE_SIZE = 50;
const distance = (value: number) =>
  `${value.toLocaleString("en-US", { maximumFractionDigits: 1 })} km`;
const title = (stop: TripStop) =>
  readablePreview(stop.title) || "Recorded stop";
function time(value?: string | null) {
  if (!value) return "Time unknown";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "Time unknown"
    : date.toLocaleString("en-US", {
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
        timeZone: "UTC",
      });
}

export default function TripsWorkspace({
  refreshKey,
  onOpenEvent,
  onShowMap,
}: Props) {
  const [filters, setFilters] = useState({
    from: "",
    to: "",
    source: "",
    kind: "all",
  });
  const [sources, setSources] = useState<{ id: string; name: string }[]>([]);
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<JourneysPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const [selectedId, setSelectedId] = useState("");
  const [legOffset, setLegOffset] = useState(0);
  const [detail, setDetail] = useState<JourneyDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");
  const [detailRetry, setDetailRetry] = useState(0);
  const { from, to, source, kind } = filters;

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    setPage(null);
    const params = new URLSearchParams({
      limit: String(PAGE_SIZE),
      offset: String(offset),
      kind,
    });
    for (const [key, value] of Object.entries({ from, to, source }))
      if (value) params.set(key, value);
    api<JourneysPage>(`/journeys?${params}`, { signal: controller.signal })
      .then((result) => {
        if (controller.signal.aborted) return;
        setPage(result);
        setSources((previous) => [
          ...new Map(
            [...previous, ...result.sources].map((item) => [item.id, item]),
          ).values(),
        ]);
        setSelectedId((previous) =>
          result.items.some((item) => item.id === previous)
            ? previous
            : result.items[0]?.id || "",
        );
        setLegOffset(0);
      })
      .catch((cause) => {
        if (!controller.signal.aborted)
          setError(
            cause instanceof Error ? cause.message : "Trips could not load.",
          );
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [from, to, source, kind, offset, refreshKey, retry]);

  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      setDetailLoading(false);
      return;
    }
    const controller = new AbortController();
    setDetailLoading(true);
    setDetailError("");
    setDetail(null);
    api<JourneyDetail>(
      `/journeys/${encodeURIComponent(selectedId)}?limit=${LEG_PAGE_SIZE}&offset=${legOffset}`,
      { signal: controller.signal },
    )
      .then((result) => {
        if (!controller.signal.aborted) setDetail(result);
      })
      .catch((cause) => {
        if (!controller.signal.aborted)
          setDetailError(
            cause instanceof Error ? cause.message : "Trip could not load.",
          );
      })
      .finally(() => {
        if (!controller.signal.aborted) setDetailLoading(false);
      });
    return () => controller.abort();
  }, [selectedId, legOffset, refreshKey, detailRetry]);

  function filter(key: keyof typeof filters, value: string) {
    setFilters((previous) => ({ ...previous, [key]: value }));
    setOffset(0);
    setSelectedId("");
    setLegOffset(0);
  }
  function select(id: string) {
    setSelectedId(id);
    setLegOffset(0);
  }
  function showMap(journey: Journey) {
    onShowMap({
      from: (journey.first_stop.occurred_at || journey.departure_at).slice(
        0,
        10,
      ),
      to: (journey.last_stop.occurred_at || journey.arrival_at).slice(0, 10),
      sourceId: journey.source_id,
      tripId: journey.id,
    });
  }
  const selected =
    detail?.journey || page?.items.find((item) => item.id === selectedId);

  return (
    <section className="trips-workspace" aria-label="Trips">
      <div className="trips-heading">
        <div>
          <h2>
            <Route size={19} /> Trips
          </h2>
          <p>Inferred connections between recorded stops.</p>
        </div>
        <span>{loading ? "Loading…" : `${count(page?.total)} trips`}</span>
      </div>
      <div className="trips-filters">
        <label>
          From
          <input
            aria-label="Trips from"
            type="date"
            value={from}
            onChange={(event) => filter("from", event.target.value)}
          />
        </label>
        <label>
          To
          <input
            aria-label="Trips to"
            type="date"
            value={to}
            onChange={(event) => filter("to", event.target.value)}
          />
        </label>
        <label>
          Source
          <select
            aria-label="Trip source"
            value={source}
            onChange={(event) => filter("source", event.target.value)}
          >
            <option value="">All sources</option>
            {sources.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Distance
          <select
            aria-label="Trip distance"
            value={kind}
            onChange={(event) => filter("kind", event.target.value)}
          >
            <option value="all">All distances</option>
            <option value="long_distance">
              Includes a connection of 100 km or more
            </option>
            <option value="local_movement">Connections under 100 km</option>
          </select>
        </label>
      </div>
      {error && (
        <div className="trips-error" role="alert">
          {error}{" "}
          <button onClick={() => setRetry((value) => value + 1)}>
            Retry trips
          </button>
        </div>
      )}
      {page && !page.coverage_complete && (
        <p className="trips-notice" role="status">
          Some source histories could not be connected. These results cover the
          usable recorded stops.
        </p>
      )}
      {loading ? (
        <p role="status">Loading trips…</p>
      ) : page?.items.length === 0 ? (
        <div className="trips-empty">
          <h3>No trips in this selection</h3>
          <p>Missing or uncertain records remain unconnected.</p>
        </div>
      ) : (
        page && (
          <div className="trips-layout">
            <div className="trips-list-column">
              <div className="trips-list" aria-label="Inferred trips">
                {page.items.map((journey) => (
                  <button
                    type="button"
                    className={`journey-card ${selectedId === journey.id ? "selected" : ""}`}
                    aria-pressed={selectedId === journey.id}
                    key={journey.id}
                    onClick={() => select(journey.id)}
                  >
                    <span className="journey-date">
                      {formatDate(journey.departure_at)}
                      {journey.departure_at.slice(0, 10) !==
                        journey.arrival_at.slice(0, 10) &&
                        ` → ${formatDate(journey.arrival_at)}`}
                    </span>
                    <strong>
                      {title(journey.first_stop)}{" "}
                      <ArrowRight size={13} aria-label="to" />{" "}
                      {title(journey.last_stop)}
                    </strong>
                    <span className="journey-source">{journey.source}</span>
                    <span className="journey-counts">
                      {count(journey.represented_stops)} stops ·{" "}
                      {distance(journey.distance_km)} inferred
                    </span>
                  </button>
                ))}
              </div>
              <div className="trips-pagination">
                <button
                  aria-label="Previous trips"
                  disabled={offset === 0}
                  onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                >
                  <ChevronLeft size={15} /> Previous
                </button>
                <span>
                  {count(offset + 1)}–{count(offset + page.items.length)} of{" "}
                  {count(page.total)}
                </span>
                <button
                  aria-label="Next trips"
                  disabled={offset + page.items.length >= page.total}
                  onClick={() => setOffset(offset + PAGE_SIZE)}
                >
                  Next <ChevronRight size={15} />
                </button>
              </div>
            </div>
            <section className="journey-detail" aria-label="Selected trip">
              {selected && (
                <>
                  <div className="journey-detail-heading">
                    <div>
                      <span>{selected.source}</span>
                      <h3>
                        {title(selected.first_stop)}{" "}
                        <ArrowRight size={17} aria-label="to" />{" "}
                        {title(selected.last_stop)}
                      </h3>
                      <p>
                        {formatDate(selected.departure_at)} →{" "}
                        {formatDate(selected.arrival_at)}
                      </p>
                    </div>
                    <button onClick={() => showMap(selected)}>
                      <MapIcon size={15} /> View on map
                    </button>
                  </div>
                  <div className="journey-metrics">
                    <span>
                      <strong>{count(selected.represented_stops)}</strong>{" "}
                      represented stops
                    </span>
                    <span>
                      <strong>{count(selected.legs)}</strong> connections
                    </span>
                    <span>
                      <strong>{distance(selected.distance_km)}</strong>{" "}
                      straight-line total
                    </span>
                  </div>
                  <p className="journey-uncertainty">
                    Inferred grouping, not a recorded route. Travel mode and
                    intermediate stops are unknown. Short movements may be
                    omitted.
                  </p>
                </>
              )}
              {selected && <TripSummary key={selected.id} tripId={selected.id} departure={selected.departure_at} arrival={selected.arrival_at} onOpenEvent={onOpenEvent} />}
              {detailLoading && <p role="status">Loading recorded stops…</p>}
              {detailError && (
                <p className="trips-error" role="alert">
                  {detailError}{" "}
                  <button onClick={() => setDetailRetry((value) => value + 1)}>
                    Retry trip
                  </button>
                </p>
              )}
              {detail && (
                <>
                  <div className="journey-connections-heading">
                    <h4>Recorded stops</h4>
                    <span>Times in UTC</span>
                  </div>
                  <ol className="journey-connections" start={legOffset + 1}>
                    {detail.items.map((leg) => (
                      <li key={leg.id}>
                        <div className="journey-stop-pair">
                          {([leg.from, leg.to] as EvidenceStop[]).map(
                            (stop, index) => (
                              <div
                                className="journey-stop"
                                key={`${index}-${stop.event_id}`}
                              >
                                <span
                                  className="journey-stop-mark"
                                  aria-hidden="true"
                                />
                                <button
                                  onClick={() => onOpenEvent(stop.event_id)}
                                >
                                  {title(stop)}
                                  <small>
                                    {time(
                                      index === 0
                                        ? leg.departure_at || stop.end_at
                                        : leg.arrival_at || stop.occurred_at,
                                    )}
                                  </small>
                                </button>
                                <button
                                  className="journey-evidence-link"
                                  onClick={() => onOpenEvent(stop.event_id)}
                                >
                                  Open record
                                  {stop.provenance_total ? (
                                    <small>
                                      {count(stop.provenance_total)}{" "}
                                      {stop.provenance_total === 1
                                        ? "source reference"
                                        : "source references"}
                                    </small>
                                  ) : null}
                                </button>
                              </div>
                            ),
                          )}
                        </div>
                        <p>
                          {distance(leg.distance_km)} straight-line ·{" "}
                          {leg.gap_hours.toLocaleString("en-US", {
                            maximumFractionDigits: 1,
                          })}{" "}
                          hours between stops
                        </p>
                      </li>
                    ))}
                  </ol>
                  {!detail.complete && (
                    <p className="trips-notice">
                      Showing connections{" "}
                      {detail.items.length ? count(legOffset + 1) : "0"}–
                      {count(legOffset + detail.items.length)} of{" "}
                      {count(detail.total)}. This page is part of the trip.
                    </p>
                  )}
                  <div className="trips-pagination">
                    <button
                      aria-label="Previous connections"
                      disabled={legOffset === 0}
                      onClick={() =>
                        setLegOffset(Math.max(0, legOffset - LEG_PAGE_SIZE))
                      }
                    >
                      <ChevronLeft size={15} /> Previous
                    </button>
                    <span>{count(detail.total)} connections</span>
                    <button
                      aria-label="Next connections"
                      disabled={legOffset + detail.items.length >= detail.total}
                      onClick={() => setLegOffset(legOffset + LEG_PAGE_SIZE)}
                    >
                      Next <ChevronRight size={15} />
                    </button>
                  </div>
                </>
              )}
            </section>
          </div>
        )
      )}
    </section>
  );
}
