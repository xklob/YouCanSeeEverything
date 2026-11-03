import { ArrowUpRight, CalendarDays, MapPin, SearchCheck } from "lucide-react";
import { formatDate, humanize, type LifeEvent } from "./api";

interface EnrichmentLocation {
  venue?: string;
  address?: string;
  city?: string;
  region?: string;
  country?: string;
  latitude?: number;
  longitude?: number;
}
interface PublicEvidence {
  url?: string;
  kind?: string;
  evidence?: string;
  access_status?: string | number;
}
export interface EventEnrichment {
  location?: EnrichmentLocation;
  method?: string;
  confidence?: string;
  time_precision?: string;
  notes?: string[];
  provenance?: PublicEvidence[];
  original_values?: {
    occurred_at?: string | null;
    end_at?: string | null;
    time_precision?: string | null;
  };
  reviewed_at?: string;
}

export function eventEnrichment(event: LifeEvent): EventEnrichment | undefined {
  const value = event.metadata?.enrichment;
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as EventEnrichment)
    : undefined;
}

function text(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}
function placeParts(location?: EnrichmentLocation, compact = false): string[] {
  if (!location || typeof location !== "object") return [];
  const values = compact
    ? [location.venue, location.city, location.region]
    : [
        location.venue,
        location.address,
        location.city,
        location.region,
        location.country,
      ];
  return [...new Set(values.map(text).filter(Boolean))];
}

export function publicEvidenceUrl(value: unknown): string | null {
  if (typeof value !== "string") return null;
  try {
    const url = new URL(value);
    const hostname = url.hostname.toLowerCase();
    if (
      !["https:", "http:"].includes(url.protocol) ||
      url.username ||
      url.password ||
      !hostname.includes(".") ||
      /^[\d.]+$/.test(hostname) ||
      hostname.includes(":") ||
      /(?:^|\.)(?:localhost|local|internal|test|invalid)$/.test(hostname)
    )
      return null;
    return url.href;
  } catch {
    return null;
  }
}

function available(source: PublicEvidence): boolean {
  if (typeof source.access_status === "number")
    return source.access_status >= 200 && source.access_status < 400;
  return !/(?:failed|unavailable|blocked|forbidden|not.?found|timeout|error|(?:http[_ -]?)?[45]\d\d)/i.test(
    text(source.access_status),
  );
}

function methodLabel(method?: string): string {
  if (!method) return "Reviewed public sources";
  const known: Record<string, string> = {
    manual_public_research: "Reviewed public event sources",
    public_event_research: "Reviewed public event sources",
    manual_web_research: "Reviewed public event sources",
    reviewed_public_sources: "Reviewed public sources",
    public_event_page: "Public event page review",
  };
  return known[method] || humanize(method);
}

export function EnrichedLocation({ event }: { event: LifeEvent }) {
  const place = placeParts(eventEnrichment(event)?.location, true);
  return place.length ? (
    <p className="enriched-location">
      <MapPin size={12} />
      <span>{place.join(" · ")}</span>
      <span className="reviewed-place-label">Reviewed place</span>
    </p>
  ) : null;
}

export default function EnrichmentEvidence({ event }: { event: LifeEvent }) {
  const enrichment = eventEnrichment(event);
  if (!enrichment) return null;
  const place = placeParts(enrichment.location);
  const sources = Array.isArray(enrichment.provenance)
    ? enrichment.provenance.filter(
        (source) => source && typeof source === "object",
      )
    : [];
  const supporting = sources.filter(
    (source) => available(source) && publicEvidenceUrl(source.url),
  );
  const unavailable = sources.filter((source) => !available(source));
  const original = enrichment.original_values;
  const originalUnknown =
    !original?.occurred_at || original.time_precision === "unknown";
  const registration =
    /eventbrite/i.test(event.source) || /registration/i.test(event.category);
  const notes = Array.isArray(enrichment.notes)
    ? enrichment.notes.filter(
        (note): note is string =>
          typeof note === "string" && Boolean(note.trim()),
      )
    : [];

  return (
    <section
      className="enrichment-evidence"
      aria-label="Date and place evidence"
    >
      <h3>
        <SearchCheck size={16} />
        Date and place evidence
      </h3>
      <div className="enrichment-method">
        <span>{methodLabel(enrichment.method)}</span>
        {enrichment.confidence === "high" && (
          <span className="review-confidence">High confidence</span>
        )}
      </div>
      <div className="reviewed-event-facts">
        {event.occurred_at && (
          <p>
            <CalendarDays size={14} />
            <span>
              {formatDate(
                event.occurred_at,
                enrichment.time_precision || event.time_precision,
              )}
              {event.end_at &&
                ` – ${formatDate(event.end_at, enrichment.time_precision || event.time_precision)}`}
            </span>
          </p>
        )}
        {place.length > 0 && (
          <p>
            <MapPin size={14} />
            <span>{place.join(", ")}</span>
          </p>
        )}
      </div>
      {original && (
        <p className="original-date-note">
          {originalUnknown
            ? "Original record date: unknown."
            : `Original record date: ${formatDate(original.occurred_at, original.time_precision || undefined)}.`}{" "}
          {event.occurred_at
            ? "The timeline date comes from the reviewed evidence below."
            : ""}
        </p>
      )}
      {registration && (
        <p className="registration-context">
          Registration does not verify physical attendance.
        </p>
      )}
      {supporting.length > 0 && (
        <ul className="public-evidence-list">
          {supporting.map((source, index) => (
            <li key={`${source.url}-${index}`}>
              <a
                href={publicEvidenceUrl(source.url)!}
                target="_blank"
                rel="noopener noreferrer"
              >
                <span>
                  {source.kind ? humanize(source.kind) : "Public source"}
                </span>
                <ArrowUpRight size={13} />
              </a>
              {text(source.evidence) && <p>{text(source.evidence)}</p>}
              <span>{new URL(source.url!).hostname}</span>
            </li>
          ))}
        </ul>
      )}
      {unavailable.length > 0 && (
        <p className="unavailable-evidence">
          {unavailable
            .map((source) =>
              source.kind ? humanize(source.kind) : "Public source",
            )
            .join(", ")}
          : unavailable, not used as supporting evidence.
        </p>
      )}
      {notes.length > 0 && (
        <ul className="enrichment-notes">
          {notes.map((note, index) => (
            <li key={index}>{note}</li>
          ))}
        </ul>
      )}
      {enrichment.reviewed_at && (
        <p className="enrichment-reviewed-at">
          Reviewed {formatDate(enrichment.reviewed_at)}
        </p>
      )}
    </section>
  );
}
