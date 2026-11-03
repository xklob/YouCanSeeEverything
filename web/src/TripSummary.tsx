import {useEffect,useRef,useState} from 'react';
import {api,formatDate,humanize,type Job,type Source,type SavedNarrative,type SavedSummaryResult} from './api';
import SummaryView from './SummaryView';

type Props={tripId:string;departure:string;arrival:string;onOpenEvent:(id:string)=>void};
export default function TripSummary(props:Props) {
  const [open,setOpen]=useState(false);
  return <details className="monthly-summary-panel" onToggle={event=>setOpen(event.currentTarget.open)}><summary>Summarize this trip</summary>{open&&<TripSummaryForm {...props}/>}</details>;
}
function TripSummaryForm({tripId,departure,arrival,onOpenEvent}:Props) {
  const [sources,setSources]=useState<Source[]>([]),[source,setSource]=useState(''),[model,setModel]=useState('gpt-6.1-sol');
  const [focus,setFocus]=useState(''),[focusQuery,setFocusQuery]=useState(''),[retry,setRetry]=useState(0);
  const [result,setResult]=useState<SavedNarrative|null>(null),[job,setJob]=useState<Job|null>(null);
  const [loading,setLoading]=useState(true),[submitting,setSubmitting]=useState(false),[error,setError]=useState('');
  const generation=useRef(false),epoch=useRef(0);
  const pending=!!job&&['queued','pending','running'].includes(job.status),busy=submitting||pending;
  useEffect(()=>{const controller=new AbortController();api<{items:Source[]}>('/sources',{signal:controller.signal}).then(value=>{if(!controller.signal.aborted)setSources(value.items)}).catch(cause=>{if(!controller.signal.aborted)setError(cause.message)});return ()=>controller.abort()},[]);
  useEffect(()=>{
    if(generation.current)return;
    const controller=new AbortController(),current=++epoch.current;
    setLoading(true);setResult(null);setError('');setJob(null);
    const params=new URLSearchParams({trip_id:tripId,model});if(source)params.set('source',source);if(focusQuery)params.set('focus',focusQuery);
    api<SavedSummaryResult>(`/summaries/trip?${params}`,{signal:controller.signal})
      .then(value=>{if(!controller.signal.aborted&&current===epoch.current)setResult(value.result)})
      .catch(cause=>{if(!controller.signal.aborted&&current===epoch.current)setError(cause.message)})
      .finally(()=>{if(!controller.signal.aborted&&current===epoch.current)setLoading(false)});
    return ()=>controller.abort();
  },[tripId,source,model,focusQuery,retry]);
  useEffect(()=>{
    if(!pending||!job)return;
    const controller=new AbortController();let timer:number;
    const poll=async()=>{
      try {
        const next=await api<Job>(`/jobs/${encodeURIComponent(job.id)}`,{signal:controller.signal});if(controller.signal.aborted)return;
        setJob(next);
        if(['queued','pending','running'].includes(next.status))timer=window.setTimeout(()=>void poll(),2500);
        else {generation.current=false;if(next.status==='completed'&&(next.result as SavedNarrative)?.kind==='trip_summary')setResult(next.result as SavedNarrative);if(next.error)setError(next.error)}
      }catch(cause){if(!controller.signal.aborted){generation.current=false;setError((cause as Error).message);setJob(null)}}
    };void poll();return ()=>{controller.abort();window.clearTimeout(timer)};
  },[job?.id,pending]);
  const generate=async()=>{
    if(busy)return;generation.current=true;epoch.current++;setLoading(false);setSubmitting(true);setError('');setFocusQuery(focus.trim());
    try {const created=await api<{id:string;status:string}>('/assistant',{method:'POST',body:JSON.stringify({mode:'summarize_trip',trip_id:tripId,source_ids:source?[source]:[],prompt:focus.trim(),model})});setJob({...created,kind:'summarize_trip',prompt:focus.trim(),created_at:new Date().toISOString()})}
    catch(cause){generation.current=false;setError((cause as Error).message)}finally{setSubmitting(false)}
  };
  return <div className="monthly-summary-content">
    <p>{formatDate(departure)} → {formatDate(arrival)} · UTC date window</p>
    <p>Recorded stops set the window. Other records in these dates do not establish trip activity or presence. Route and travel mode are unknown.</p>
    <p>Generating sends selected excerpts to Codex. Opening does not run a model.</p>
    <div className="monthly-controls"><label>Sources<select aria-label="Trip summary sources" value={source} disabled={busy} onChange={event=>setSource(event.target.value)}><option value="">All sources</option>{sources.map(item=><option key={item.id} value={item.id}>{item.name}</option>)}</select></label><label>Model<select aria-label="Trip summary model" value={model} disabled={busy} onChange={event=>setModel(event.target.value)}><option value="gpt-6.1-sol">Sol · high</option><option value="gpt-6-astra">Astra · xhigh</option></select></label></div>
    <label className="summary-focus">Optional focus<input aria-label="Trip summary focus" value={focus} disabled={busy} maxLength={4000} onChange={event=>setFocus(event.target.value)} onBlur={()=>setFocusQuery(focus.trim())}/></label>
    <div className="summary-generate-row"><span>{loading?'Checking saved summary…':busy?`Codex ${humanize(job?.status||'starting')}…`:result?'Saved · Sources not checked':'No saved summary for this selection'}</span><button disabled={busy} onClick={()=>void generate()}>{busy?'Working…':'Generate trip summary'}</button></div>
    {error&&<p role="alert">{error} <button disabled={busy} onClick={()=>setRetry(n=>n+1)}>Recheck trip summary</button></p>}
    <SummaryView result={result} onOpenEvent={onOpenEvent}/>
  </div>;
}
