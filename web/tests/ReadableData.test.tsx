import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ReadableData, { CitedProse, readablePreview } from "../src/ReadableData";
afterEach(cleanup);
it("renders JSON-shaped metadata and notes as labeled content without JSON syntax", () => {
  render(
    <ReadableData
      value={
        '{"source_name":"Watch export","notes":["Original retained","Unit known"],"sampleCount":3,"verified":true}'
      }
    />,
  );
  expect(screen.getByText("Source name")).toBeTruthy();
  expect(screen.getByText("Sample count")).toBeTruthy();
  expect(screen.getByText("Watch export")).toBeTruthy();
  expect(screen.getByText("Yes")).toBeTruthy();
  expect(document.body.textContent).not.toContain("{");
  expect(document.body.textContent).not.toContain('["');
  expect(readablePreview('{"step_count":3}')).toBe("Step count: 3");
});
it("turns known citation IDs into evidence actions while preserving ordinary source text", async () => {
  const open = vi.fn();
  const user = userEvent.setup();
  render(
    <CitedProse
      text="A supported observation [record-id]."
      citations={["record-id"]}
      onEvidence={open}
    />,
  );
  await user.click(screen.getByRole("button", { name: "Open cited record 1" }));
  expect(open).toHaveBeenCalledWith("record-id");
  expect(document.body.textContent).not.toContain("[record-id]");
});

it("hides incomplete JSON syntax behind a source-file prompt without changing prose or code", () => {
  const truncated = '{"sample_count":3,"samples":[{"value":72,"note":"cut off';
  render(<ReadableData value={truncated} />);
  expect(
    screen.getByText(
      "Structured preview is incomplete. Open the source file for the full record.",
    ),
  ).toBeTruthy();
  expect(document.body.textContent).not.toContain("{");
  expect(readablePreview(truncated)).toContain(
    "Structured preview is incomplete",
  );
  expect(readablePreview("Keep [these brackets] in ordinary prose.")).toBe(
    "Keep [these brackets] in ordinary prose.",
  );
  const code = '{"handler": function() { return 1; }';
  expect(readablePreview(code)).toBe(code);
});
