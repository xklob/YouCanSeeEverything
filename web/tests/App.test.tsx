import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "../src/App";

const event = {
  id: "record-one",
  title: "A recorded conversation",
  body: "Original text from a synthetic archive.",
  occurred_at: "2020-03-14T12:00:00Z",
  source_id: "sample",
  source: "Sample archive",
  category: "conversation",
  time_precision: "day",
};
const overview = {
  total_events: 85,
  sources_count: 1,
  files_count: 2,
  stored_bytes: 2048,
  date_range: { start: event.occurred_at, end: event.occurred_at },
  activity: [{ month: "2020-03", count: 85 }],
  categories: [{ name: "conversation", count: 85 }],
  undated_count: 3,
};
let requests: { path: string; options?: RequestInit }[];
let rejectEvents = false;
const fetchMock = vi.fn(
  async (input: RequestInfo | URL, options?: RequestInit) => {
    const path = String(input);
    requests.push({ path, options });
    let data: unknown;
    if (path === "/api/overview") data = overview;
    else if (path === "/api/sources")
      data = {
        items: [
          {
            id: "sample",
            name: "Sample archive",
            status: "completed",
            file_count: 2,
            event_count: 85,
            bytes: 2048,
            notes: ["One undated file remains.", "Original files retained."],
          },
        ],
      };
    else if (path.startsWith("/api/map?")) data = { items: [], total: 0 };
    else if (path === "/api/imports") data = { items: [] };
    else if (path === "/api/jobs") data = { items: [] };
    else if (path === "/api/session") data = { token: "test-session-token" };
    else if (path === "/api/assistant")
      data = { id: "job-one", status: "queued" };
    else if (path === "/api/events/record-one")
      data = {
        ...event,
        provenance: [
          {
            file_id: "file-one",
            path: "/test/archive/conversation.json",
            sha256: "abc123",
            locator: "message 12",
          },
        ],
      };
    else if (path.startsWith("/api/events?")) {
      if (rejectEvents)
        return new Response(
          JSON.stringify({
            detail: "The local archive is temporarily unavailable.",
          }),
          { status: 503 },
        );
      const params = new URL(path, "http://localhost").searchParams;
      data = {
        items: [event],
        total: 85,
        limit: 40,
        offset: Number(params.get("offset")),
      };
    } else throw new Error(`Unexpected request: ${path}`);
    return new Response(JSON.stringify(data), {
      headers: { "Content-Type": "application/json" },
    });
  },
);

beforeEach(() => {
  requests = [];
  rejectEvents = false;
  fetchMock.mockClear();
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("scrollTo", vi.fn());
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("Archive browser workflows", () => {
  it("opens the exact original evidence and closes the drawer with Escape", async () => {
    const user = userEvent.setup();
    render(<App />);
    await user.click(
      screen.getByRole("button", { name: "Timeline", exact: true }),
    );
    await user.click(
      await screen.findByRole("button", { name: /A recorded conversation/ }),
    );
    const original = await screen.findByRole("link", {
      name: "Open original file /test/archive/conversation.json",
    });
    expect(original.getAttribute("href")).toBe("/api/files/file-one/download");
    expect(screen.getByText("message 12")).toBeTruthy();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement?.textContent).toContain(
      "A recorded conversation",
    );
  });

  it("resets pagination on a search or undated filter and sends those filters to the API", async () => {
    const user = userEvent.setup();
    render(<App />);
    await user.click(
      screen.getByRole("button", { name: "Timeline", exact: true }),
    );
    await screen.findByRole("button", { name: /A recorded conversation/ });
    await user.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() =>
      expect(requests.some((item) => item.path.includes("offset=40"))).toBe(
        true,
      ),
    );
    await user.type(
      screen.getByRole("searchbox", { name: "Search your timeline" }),
      "a person",
    );
    await waitFor(() => {
      const latest = requests
        .filter((item) => item.path.startsWith("/api/events?"))
        .at(-1)!;
      expect(
        new URL(latest.path, "http://localhost").searchParams.get("q"),
      ).toBe("a person");
      expect(
        new URL(latest.path, "http://localhost").searchParams.get("offset"),
      ).toBe("0");
    });
    await user.click(screen.getByRole("checkbox", { name: /Undated only/ }));
    await waitFor(() =>
      expect(requests.some((item) => item.path.includes("undated=true"))).toBe(
        true,
      ),
    );
    expect(
      (screen.getByLabelText("From date") as HTMLInputElement).disabled,
    ).toBe(true);
    await user.click(screen.getByRole("button", { name: "Clear" }));
    await waitFor(() => {
      const latest = requests
        .filter((item) => item.path.startsWith("/api/events?"))
        .at(-1)!;
      expect(
        new URL(latest.path, "http://localhost").searchParams.has("undated"),
      ).toBe(false);
    });
  });

  it("renders source coverage notes and browses the selected source", async () => {
    const user = userEvent.setup();
    render(<App />);
    await user.click(
      screen.getByRole("button", { name: "Timeline", exact: true }),
    );
    await user.click(screen.getByRole("button", { name: "Source library" }));
    expect(await screen.findByText(/One undated file remains/)).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Browse timeline" }));
    await waitFor(() =>
      expect(requests.some((item) => item.path.includes("source=sample"))).toBe(
        true,
      ),
    );
  });

  it("makes errors recoverable without displaying sample records", async () => {
    rejectEvents = true;
    const user = userEvent.setup();
    render(<App />);
    await user.click(
      screen.getByRole("button", { name: "Timeline", exact: true }),
    );
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.queryByText(event.title)).toBeNull();
    rejectEvents = false;
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(
      await screen.findByRole("button", { name: /A recorded conversation/ }),
    ).toBeTruthy();
  });

  it("protects an assistant submission with a local session token in a header", async () => {
    const user = userEvent.setup();
    render(<App />);
    await user.click(
      screen.getByRole("button", { name: "Timeline", exact: true }),
    );
    await user.click(screen.getByRole("button", { name: /Ask your archive/ }));
    await user.type(
      screen.getByRole("textbox", { name: "Ask your archive" }),
      "What happened in March 2020?",
    );
    await user.click(screen.getByRole("button", { name: "Ask Codex" }));
    await waitFor(() =>
      expect(requests.some((item) => item.path === "/api/assistant")).toBe(
        true,
      ),
    );
    const submission = requests.find((item) => item.path === "/api/assistant")!;
    expect(submission.options?.method).toBe("POST");
    expect(
      new Headers(submission.options?.headers).get("X-Biography-Token"),
    ).toBe("test-session-token");
    expect(JSON.parse(String(submission.options?.body))).toEqual({
      prompt: "What happened in March 2020?",
      mode: "ask",
    });
    expect(
      requests.every((item) => !item.path.includes("test-session-token")),
    ).toBe(true);
  });
});
