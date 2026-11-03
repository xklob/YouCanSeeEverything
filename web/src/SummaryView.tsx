import { ArrowUpRight } from 'lucide-react';
import { count, formatDate, humanize, type SavedNarrative, type SummaryCoverage } from './api';
import { CitedProse } from './ReadableData';

export type NarrativeSummary = Pick<SavedNarrative, 'title' | 'model' | 'cache' | 'sections' | 'answer' | 'citations' | 'limitations' | 'coverage' | 'trip_context'>;
export default function SummaryView({ result, coverage = result?.coverage, onOpenEvent }: {
  result?: NarrativeSummary | null;
  coverage?: SummaryCoverage;
  onOpenEvent: (id: string) => void;
}) {
  const cite = (ids: string[]) => ids.map(id => (
    <button key={id} className="summary-citation" onClick={() => onOpenEvent(id)}
      aria-label={result?.citations.includes(id) ? `Open cited record ${result.citations.indexOf(id) + 1}` : 'Open source record'}>
      <span>{result?.citations.includes(id) ? result.citations.indexOf(id) + 1 : 'Source'}</span>
      <ArrowUpRight size={9} />
    </button>
  ));
  return <>
    {result?.trip_context && <section className="summary-trip-context" aria-label="Trip summary window">
      <p>{formatDate(result.trip_context.window_start)} → {formatDate(result.trip_context.window_end)} · UTC date window</p>
      <p>{result.trip_context.notice}</p>
      <p>{result.trip_context.coincident_notice}</p>
      <details>
        <summary>Trip evidence</summary>
        <p>Recorded-stop evidence {cite(result.trip_context.anchor_event_ids)}</p>
        <p>Records associated by time {cite(result.trip_context.coincident_event_ids)}</p>
        {(!result.trip_context.anchors_complete || !result.trip_context.journey_coverage_complete) && <p>Stop evidence is partial.</p>}
      </details>
    </section>}
    {result && <article className="period-summary-result">
      <header><h4>{result.title}</h4><span>{result.model} · {formatDate(result.cache.generated_at)}</span></header>
      {result.sections?.length ? result.sections.map((section, index) => <section key={index}>
        <h5>{section.heading}</h5>
        {section.sentences.map((sentence, index) => <p key={index}>
          {sentence.text}<span className="sentence-citations">{cite(sentence.citations)}</span>
        </p>)}
      </section>) : <CitedProse text={result.answer} citations={result.citations} onEvidence={onOpenEvent} />}
      {result.limitations?.length > 0 && <ul className="summary-limitations">
        {result.limitations.map((note, index) => <li key={index}>{note}</li>)}
      </ul>}
    </article>}
    {coverage && <details className="summary-context-coverage">
      <summary>Summary evidence coverage</summary>
      <p>
        {count(coverage.selected_events)} selected records.{' '}
        {typeof coverage.total_events_in_scope === 'number'
          ? `${count(coverage.total_events_in_scope)} records in scope.` : 'Scope count unavailable.'}
        {typeof coverage.candidate_events === 'number' && ` ${count(coverage.candidate_events)} retrieval candidates.`}
        {typeof coverage.undated_events_excluded === 'number' && ` ${count(coverage.undated_events_excluded)} undated records excluded.`}
      </p>
      {coverage.time_basis && <p>Date basis: {humanize(coverage.time_basis)}</p>}
      {result && typeof coverage.retrieval_limited === 'boolean' && <p>
        {coverage.retrieval_limited ? 'Only selected evidence was summarized.' : 'Evidence selection is shown below.'}
      </p>}
      {coverage.sources.map(item => <p key={item.source_id}>
        {item.source}: {count(item.selected_events)} selected /{' '}
        {typeof item.total_events === 'number' ? `${count(item.total_events)} in scope` : 'scope count unavailable'}
        {item.status && ` · ${humanize(item.status)}`}
      </p>)}
      {coverage.limitations?.map((note, index) => <p key={index}>{note}</p>)}
    </details>}
  </>;
}
