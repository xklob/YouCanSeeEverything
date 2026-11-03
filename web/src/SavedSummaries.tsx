import {useEffect,useState} from 'react';
import {api,count,formatDate,type SavedSummaryPage,type SavedSummaryResult} from './api';
import SummaryView from './SummaryView';
import {SortControls,useSortPreference} from './sorting';

export default function SavedSummaries({onOpenEvent}:{onOpenEvent:(id:string)=>void}) {
  const sorting=useSortPreference('saved-summaries','generated',['generated','period','title'],'desc');
  const [sort,direction]=sorting;
  const reset=()=>{setOffset(0);setSelected('')};
  const [open,setOpen]=useState(false),[offset,setOffset]=useState(0),[retry,setRetry]=useState(0);
  const [page,setPage]=useState<SavedSummaryPage|null>(null),[selected,setSelected]=useState('');
  const [saved,setSaved]=useState<SavedSummaryResult|null>(null),[error,setError]=useState('');
  const [loading,setLoading]=useState(false),[reading,setReading]=useState(false);
  useEffect(()=>{
    if(!open)return;
    const controller=new AbortController(); setLoading(true);setError('');setPage(null);
    api<SavedSummaryPage>(`/summaries/library?limit=20&offset=${offset}&sort=${sort}&direction=${direction}`,{signal:controller.signal})
      .then(value=>{if(!controller.signal.aborted)setPage(value)})
      .catch(cause=>{if(!controller.signal.aborted)setError(cause.message)})
      .finally(()=>{if(!controller.signal.aborted)setLoading(false)});
    return ()=>controller.abort();
  },[open,offset,sort,direction,retry]);
  useEffect(()=>{
    setSaved(null);setReading(false);if(!open||!selected)return;
    const controller=new AbortController();setReading(true);setError('');
    api<SavedSummaryResult>(`/summaries/saved/${encodeURIComponent(selected)}`,{signal:controller.signal})
      .then(value=>{if(!controller.signal.aborted)setSaved(value)})
      .catch(cause=>{if(!controller.signal.aborted)setError(cause.message)})
      .finally(()=>{if(!controller.signal.aborted)setReading(false)});
    return ()=>controller.abort();
  },[open,selected,retry]);
  return <details className="monthly-summary-panel" onToggle={event=>setOpen(event.currentTarget.open)}>
    <summary>Saved summaries</summary>
    {open&&<div className="monthly-summary-content">
      <SortControls label="Saved summaries" options={[["generated","Saved date"],["period","Period"],["title","Title"]]} value={[sort,direction,value=>{reset();sorting[2](value)},value=>{reset();sorting[3](value)}]}/>
      <p>Saved narratives. Sources not checked. Opening does not run a model.</p>
      {loading&&<p role="status">Loading saved summaries…</p>}
      {error&&<p role="alert">{error} <button onClick={()=>setRetry(n=>n+1)}>Retry saved summaries</button></p>}
      {page&&!page.items.length&&<p>No saved summaries.</p>}
      <div className="saved-summary-list">{page?.items.map(item=><button key={item.key} aria-pressed={selected===item.key} onClick={()=>setSelected(item.key)}>
        <strong>{item.title}</strong><span>{item.kind==='trip_summary'?'Trip':item.period||'Period'} · {formatDate(item.generated_at)} · {count(item.citation_count)} cited records</span>
        <small>{item.model} · {item.source_ids.length?`${item.source_ids.length} selected sources`:'All sources'}{item.focus?` · Focus: ${item.focus}`:''}</small>
      </button>)}</div>
      {page&&<div className="trips-pagination"><button disabled={!offset||loading} onClick={()=>setOffset(Math.max(0,offset-20))}>Previous saved summaries</button><button disabled={!page.has_more||loading} onClick={()=>setOffset(offset+20)}>Next saved summaries</button></div>}
      {reading&&<p role="status">Opening saved summary…</p>}
      {saved?.result&&<section aria-label="Saved summary"><p>Saved · Sources not checked</p><SummaryView result={saved.result} onOpenEvent={onOpenEvent}/></section>}
    </div>}
  </details>;
}
