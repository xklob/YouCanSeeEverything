import {afterEach,expect,it,vi} from 'vitest';
import {cleanup,render,screen,waitFor,within} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import SavedSummaries from '../src/SavedSummaries';
import TripSummary from '../src/TripSummary';
const coverage={total_events_in_scope:4,selected_events:2,candidate_events:3,undated_events_excluded:1,sources:[],retrieval_limited:true,time_basis:'UTC dates',limitations:[]};
const result={kind:'trip_summary',title:'Saved trip narrative',answer:'Source-supported account.',citations:['anchor','coincident'],sections:[{heading:'Retained evidence',sentences:[{text:'An observation from a record.',citations:['coincident']}]}],coverage,model:'gpt-6.1-sol',cache:{generated_at:'2026-11-03'},limitations:[],trip_context:{window_start:'2024-04-04',window_end:'2024-04-05',notice:'Inferred grouping. Route and mode are unknown.',coincident_notice:'Records in the date window do not establish trip activity or presence.',anchor_event_ids:['anchor'],coincident_event_ids:['coincident'],anchors_complete:true,journey_coverage_complete:false}};
afterEach(()=>{cleanup();vi.unstubAllGlobals()});
it('opens saved narratives and original citations with read-only bounded pagination',async()=>{
 const requests:{path:string;method?:string}[]=[];
 vi.stubGlobal('fetch',vi.fn(async(input,options)=>{
  const path=String(input);requests.push({path,method:options?.method});
  if(path.includes('/saved/'))return Response.json({status:'saved',freshness:'not_checked',result});
  const second=path.includes('offset=20');return Response.json({items:second?[]:[{key:'key/one',title:'A stored account',kind:'trip_summary',source_ids:['source'],model:'gpt-6.1-sol',generated_at:'2026-11-03',citation_count:2,focus:'people'}],limit:20,offset:second?20:0,has_more:!second});
 }));
 const open=vi.fn(),user=userEvent.setup();render(<SavedSummaries onOpenEvent={open}/>);
 expect(requests).toHaveLength(0);await user.click(screen.getByText('Saved summaries'));
 await user.click(await screen.findByRole('button',{name:/A stored account/}));
 const saved=await screen.findByRole('region',{name:'Saved summary'});
 expect(within(saved).getByText('Saved · Sources not checked')).toBeTruthy();
 await user.click(within(saved).getAllByRole('button',{name:'Open cited record 2'})[0]);expect(open).toHaveBeenCalledWith('coincident');
 expect(requests.some(item=>item.path.endsWith('/saved/key%2Fone'))).toBe(true);
 await user.click(screen.getByRole('button',{name:'Next saved summaries'}));await screen.findByText('No saved summaries.');
 expect(requests.some(item=>item.path.includes('offset=20'))).toBe(true);expect(requests.some(item=>item.method==='POST')).toBe(false);
});
it('shows trip window and source scope, and generates only on explicit request without client dates',async()=>{
 const requests:{path:string;options?:RequestInit}[]=[];
 vi.stubGlobal('fetch',vi.fn(async(input,options)=>{
  const path=String(input);requests.push({path,options});
  if(path==='/api/sources')return Response.json({items:[{id:'actual-source',name:'Readable source'}]});
  if(path==='/api/session')return Response.json({token:'synthetic'});
  if(path==='/api/assistant')return Response.json({id:'job',status:'queued'});
  if(path==='/api/jobs/job')return Response.json({id:'job',status:'completed',result});
  return Response.json({status:'missing',result:null});
 }));
 const user=userEvent.setup();render(<TripSummary tripId='journey' departure='2024-04-04T12:00:00Z' arrival='2024-04-05T08:00:00Z' onOpenEvent={vi.fn()}/>);
 expect(requests).toHaveLength(0);await user.click(screen.getByText('Summarize this trip'));
 await screen.findByText('No saved summary for this selection');
 await user.selectOptions(screen.getByLabelText('Trip summary sources'),'actual-source');
 await user.type(screen.getByLabelText('Trip summary focus'),'people');await user.tab();
 await waitFor(()=>expect(requests.some(item=>item.path.includes('focus=people')&&item.path.includes('source=actual-source'))).toBe(true));
 expect(requests.some(item=>item.options?.method==='POST')).toBe(false);
 await user.click(screen.getByRole('button',{name:'Generate trip summary'}));await screen.findByText('Saved trip narrative');
 const post=requests.find(item=>item.options?.method==='POST')!;
 expect(JSON.parse(String(post.options?.body))).toEqual({mode:'summarize_trip',trip_id:'journey',source_ids:['actual-source'],prompt:'people',model:'gpt-6.1-sol'});
 expect(screen.getByRole('region',{name:'Trip summary window'})).toBeTruthy();
 expect(screen.getByText('Records in the date window do not establish trip activity or presence.')).toBeTruthy();
});
it('loads a saved trip without generating or checking source freshness',async()=>{
 const fetcher=vi.fn(async(input)=>String(input)==='/api/sources'?Response.json({items:[]}):Response.json({status:'saved',result}));vi.stubGlobal('fetch',fetcher);
 const user=userEvent.setup();render(<TripSummary tripId='journey' departure='2024-04-04' arrival='2024-04-05' onOpenEvent={vi.fn()}/>);
 await user.click(screen.getByText('Summarize this trip'));await screen.findByText('Saved trip narrative');
 expect(screen.getByText('Saved · Sources not checked')).toBeTruthy();expect(fetcher.mock.calls.every(call=>!String(call[0]).includes('/assistant'))).toBe(true);
});
