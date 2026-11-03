import { afterEach, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "../src/App";

vi.mock("../src/MapWorkspace", () => ({ default: () => null }));
const record = {
  id: "known-record",
  title: "An actual retained record",
  body: "Source evidence.",
  occurred_at: "2024-01-01",
  source_id: "archive",
  source: "Archive",
  category: "conversation",
};
const snapshot = (status = "pending") => ({
  total_events: status === "stale" ? 123 : null,
  sources_count: 1,
  files_count: 2,
  stored_bytes: null,
  date_range: { start: "2020-01-01", end: "2024-01-01" },
  activity: [],
  categories: [{ name: "conversation", count: null }],
  undated_count: null,
  stats: {
    status,
    measured_at: status === "stale" ? "2026-11-03" : null,
    categories_status: "names_only",
    activity_status: "not_computed",
    error: status === "partial" ? { code: "summary_budget_exceeded" } : null,
  },
});
const source = (status = "pending") => ({
  id: "archive",
  name: "Archive",
  status: "ready",
  file_count: 2,
  event_count: status === "stale" ? 99 : null,
  bytes: 100,
  first_event: null,
  last_event: null,
  notes: [],
  stats: { status, measured_at: status === "stale" ? "2026-11-03" : null },
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

it("browses unknown totals with has_more and labels cached counts without inventing zeros or empty dates", async () => {
  let status = "pending";
  const paths: string[] = [];
  vi.stubGlobal("scrollTo", vi.fn());
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      paths.push(path);
      let data;
      if (path === "/api/overview") data = snapshot(status);
      else if (path === "/api/sources") data = { items: [source(status)] };
      else if (path === "/api/imports") data = { items: [] };
      else if (path.startsWith("/api/events?")) {
        const offset = Number(
          new URL(path, "http://localhost").searchParams.get("offset"),
        );
        data = {
          items: [record],
          total: offset ? 1000 : null,
          has_more: !offset,
          total_status: offset ? "stale" : "pending",
          counted_at: offset ? "2026-11-03" : null,
          limit: 40,
          offset,
        };
      } else throw new Error(`Unexpected path: ${path}`);
      return new Response(JSON.stringify(data));
    }),
  );
  const user = userEvent.setup();
  render(<App />);
  await screen.findByText("2020 – 2024");
  await user.click(
    within(screen.getByRole("group", { name: "Archive view" })).getByRole(
      "button",
      { name: "Records", exact: true },
    ),
  );
  await screen.findByRole("button", { name: /An actual retained record/ });
  expect(screen.getByText("1 shown")).toBeTruthy();
  expect(screen.getByRole("option", { name: "Conversation" })).toBeTruthy();
  expect(screen.queryByRole("option", { name: /Unknown/ })).toBeNull();
  await user.click(screen.getByRole("button", { name: "Next", exact: true }));
  await waitFor(() =>
    expect(paths.some((path) => path.includes("offset=40"))).toBe(true),
  );
  await screen.findByText("41–41 · cached total 1,000");
  expect(
    (
      screen.getByRole("button", {
        name: "Next",
        exact: true,
      }) as HTMLButtonElement
    ).disabled,
  ).toBe(true);
  expect(screen.getByText("Cached counts")).toBeTruthy();
  await user.click(
    within(screen.getByRole("navigation")).getByRole("button", {
      name: "Sources",
      exact: true,
    }),
  );
  const card = document.querySelector(".source-card")!;
  expect(card.textContent).toContain("Records: Counting…");
  expect(within(card as HTMLElement).getByText("Dates pending")).toBeTruthy();
  expect(screen.queryByText("No dated records")).toBeNull();
  status = "stale";
  await user.click(screen.getByRole("button", { name: "Refresh archive" }));
  await waitFor(() => expect(card.textContent).toContain("Records: 99"));
  expect(within(card as HTMLElement).getByText("Cached counts")).toBeTruthy();
  status = "partial";
  await user.click(screen.getByRole("button", { name: "Refresh archive" }));
  await waitFor(() =>
    expect(
      within(card as HTMLElement).getByText("Partial counts"),
    ).toBeTruthy(),
  );
  expect(card.textContent).toContain("Records: Count unavailable");
});

it("keeps one abortable library cycle in flight and waits thirty seconds after it completes", async () => {
  vi.useFakeTimers();
  const pending: ((response: Response) => void)[] = [];
  const fetch = vi.fn((input: RequestInfo | URL, options?: RequestInit) =>
    String(input) === "/api/overview"
      ? new Promise<Response>((resolve) => pending.push(resolve))
      : Promise.resolve(new Response(JSON.stringify({ items: [] }))),
  );
  vi.stubGlobal("fetch", fetch);
  const mounted = render(<App />);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(120000);
  });
  expect(
    fetch.mock.calls.filter(([path]) => path === "/api/overview"),
  ).toHaveLength(1);
  await act(async () => {
    pending[0](new Response(JSON.stringify(snapshot())));
  });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(29999);
  });
  expect(
    fetch.mock.calls.filter(([path]) => path === "/api/overview"),
  ).toHaveLength(1);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1);
  });
  expect(
    fetch.mock.calls.filter(([path]) => path === "/api/overview"),
  ).toHaveLength(2);
  const activeSignal = fetch.mock.calls.find(
    ([path]) => path === "/api/overview",
  )?.[1]?.signal;
  mounted.unmount();
  expect(activeSignal?.aborted).toBe(true);
  await act(async () => {
    pending[1](new Response(JSON.stringify(snapshot())));
    await vi.advanceTimersByTimeAsync(60000);
  });
  expect(
    fetch.mock.calls.filter(([path]) => path === "/api/overview"),
  ).toHaveLength(2);
});

it("shows a counted lower bound until exact totals arrive, with per-source scope", async () => {
  localStorage.clear();
  let complete = false;
  vi.stubGlobal('fetch', vi.fn(async (input) => {
    const path = String(input);
    if (path === '/api/overview') return Response.json({ ...snapshot(), total_events: complete ? 1300 : null,
      stats: { status: complete ? 'current' : 'counting', counted_events: 1234, measured_at: null } });
    if (path === '/api/sources') return Response.json({items: [{...source(), event_count: complete ? 40 : null,
      stats: { status: complete ? 'current' : 'counting', counted_events: 12, measured_at: null }}]});
    return Response.json({items:[]});
  }));
  const user = userEvent.setup();render(<App />);
  await screen.findByText('1,234+ (counting)');
  expect(document.querySelector('.archive-summary')?.textContent).toContain('Records: 1,234+ (counting)');
  await user.click(within(screen.getByRole('navigation')).getByRole('button',{name:'Sources',exact:true}));
  const card = document.querySelector('.source-card')!;
  expect(card.textContent).toContain('Records: 12+ (counting)');
  expect(card.textContent).not.toContain('1,234');
  complete=true;await user.click(screen.getByRole('button',{name:'Refresh archive'}));
  await waitFor(()=>expect(card.textContent).toContain('Records: 40'));
  expect(card.textContent).not.toContain('(counting)');
  localStorage.clear();
});

it("preserves import order across interleaved months instead of regrouping the page", async () => {
  localStorage.clear();
  const rows = [
    {...record,id:'a',title:'Sort fixture A',occurred_at:'2024-03-01'},
    {...record,id:'b',title:'Sort fixture B',occurred_at:'2024-01-01'},
    {...record,id:'c',title:'Sort fixture C',occurred_at:'2024-03-02'},
  ];
  vi.stubGlobal('fetch',vi.fn(async(input)=>{
    const path=String(input);
    if(path==='/api/overview')return Response.json(snapshot());
    if(path==='/api/sources')return Response.json({items:[source()]});
    if(path.startsWith('/api/events?')) {
      const params=new URL(path,'http://localhost').searchParams;
      return Response.json({items:params.get('direction')==='asc'?rows:[...rows].reverse(),total:3,has_more:false,limit:40,offset:0});
    }
    return Response.json({items:[]});
  }));
  const user=userEvent.setup();render(<App/>);
  await user.click(within(screen.getByRole('group',{name:'Archive view'})).getByRole('button',{name:'Records',exact:true}));
  await screen.findByRole('button',{name:/Sort fixture C/});
  await user.selectOptions(screen.getByLabelText('Records sort'),'imported');
  const titles=()=>screen.getAllByRole('button',{name:/Sort fixture/}).map(button=>button.querySelector('h3')?.textContent);
  await waitFor(()=>expect(titles()).toEqual(['Sort fixture C','Sort fixture B','Sort fixture A']));
  await user.selectOptions(screen.getByLabelText('Records direction'),'asc');
  await waitFor(()=>expect(titles()).toEqual(['Sort fixture A','Sort fixture B','Sort fixture C']));
  localStorage.clear();
});
