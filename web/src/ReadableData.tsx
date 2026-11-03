import type { ReactNode } from "react";
import { humanize } from "./api";

const INCOMPLETE_SOURCE = Symbol("incomplete structured source");
const INCOMPLETE_LABEL =
  "Structured preview is incomplete. Open the source file for the full record.";
function incompleteJson(text: string): boolean {
  if (!/^(?:\{\s*"|\[\s*(?:\{|"|-?\d|true\b|false\b|null\b))/.test(text))
    return false;
  let index = 0;
  const space = () => {
    while (/\s/.test(text[index] || "") && index < text.length) index++;
  };
  const need = () => {
    if (index >= text.length) throw "incomplete";
  };
  const string = () => {
    if (text[index++] !== '"') throw "invalid";
    while (index < text.length) {
      const char = text[index++];
      if (char === '"') return;
      if (char.charCodeAt(0) < 32) throw "invalid";
      if (char === "\\") {
        need();
        const escaped = text[index++];
        if (escaped === "u") {
          for (let n = 0; n < 4; n++) {
            need();
            if (!/[\da-f]/i.test(text[index++])) throw "invalid";
          }
        } else if (!'"\\/bfnrt'.includes(escaped)) throw "invalid";
      }
    }
    throw "incomplete";
  };
  const value = (depth: number): void => {
    if (depth > 64) throw "invalid";
    space();
    need();
    const char = text[index];
    if (char === '"') {
      string();
      return;
    }
    if (char === "{") {
      index++;
      space();
      need();
      if (text[index] === "}") {
        index++;
        return;
      }
      while (true) {
        space();
        need();
        if (text[index] !== '"') throw "invalid";
        string();
        space();
        need();
        if (text[index++] !== ":") throw "invalid";
        value(depth + 1);
        space();
        need();
        const next = text[index++];
        if (next === "}") return;
        if (next !== ",") throw "invalid";
      }
    }
    if (char === "[") {
      index++;
      space();
      need();
      if (text[index] === "]") {
        index++;
        return;
      }
      while (true) {
        value(depth + 1);
        space();
        need();
        const next = text[index++];
        if (next === "]") return;
        if (next !== ",") throw "invalid";
      }
    }
    for (const literal of ["true", "false", "null"])
      if (char === literal[0]) {
        const remaining = text.slice(index);
        if (remaining.length < literal.length && literal.startsWith(remaining))
          throw "incomplete";
        if (!remaining.startsWith(literal)) throw "invalid";
        index += literal.length;
        return;
      }
    const number = /^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/.exec(
      text.slice(index),
    );
    if (number) {
      index += number[0].length;
      return;
    }
    throw "invalid";
  };
  try {
    value(0);
    space();
    return false;
  } catch (error) {
    return error === "incomplete";
  }
}

function decoded(value: unknown): unknown {
  if (typeof value !== "string") return value;
  const trimmed = value.trim();
  if (
    (trimmed.startsWith("{") && trimmed.endsWith("}")) ||
    (trimmed.startsWith("[") && trimmed.endsWith("]"))
  ) {
    try {
      return JSON.parse(trimmed);
    } catch {
      /* Plain source text remains text. */
    }
  }
  if (incompleteJson(trimmed)) return INCOMPLETE_SOURCE;
  return value;
}
const label = (value: string) =>
  humanize(value.replace(/([a-z\d])([A-Z])/g, "$1 $2").toLowerCase());
const scalar = (value: unknown): string =>
  value == null
    ? "Not supplied"
    : typeof value === "boolean"
      ? value
        ? "Yes"
        : "No"
      : typeof value === "number"
        ? value.toLocaleString("en-US", { maximumFractionDigits: 6 })
        : String(value);
export function readablePreview(value: unknown): string {
  const data = decoded(value);
  if (data === INCOMPLETE_SOURCE) return INCOMPLETE_LABEL;
  if (Array.isArray(data))
    return data
      .slice(0, 5)
      .map((item) =>
        typeof item === "object" && item !== null
          ? readablePreview(item)
          : scalar(item),
      )
      .join(" · ");
  if (data && typeof data === "object")
    return Object.entries(data)
      .slice(0, 5)
      .map(
        ([key, item]) =>
          `${label(key)}: ${Array.isArray(item) ? `${item.length} items` : item && typeof item === "object" ? "Source details" : scalar(item)}`,
      )
      .join(" · ");
  return typeof data === "string" ? data : scalar(data);
}

export default function ReadableData({
  value,
  depth = 0,
}: {
  value: unknown;
  depth?: number;
}) {
  const data = decoded(value);
  if (data === INCOMPLETE_SOURCE)
    return <span className="readable-nested-note">{INCOMPLETE_LABEL}</span>;
  if (data == null || typeof data !== "object")
    return <span className="readable-scalar">{scalar(data)}</span>;
  if (depth >= 5)
    return (
      <span className="readable-nested-note">
        Additional nested details are retained in the original source.
      </span>
    );
  if (Array.isArray(data))
    return data.length ? (
      <>
        <ul className="readable-list">
          {data.slice(0, 60).map((item, index) => (
            <li key={index}>
              <ReadableData value={item} depth={depth + 1} />
            </li>
          ))}
        </ul>
        {data.length > 60 && (
          <p className="readable-nested-note">
            Showing 60 of {data.length.toLocaleString()} entries. The original
            source retains the full list.
          </p>
        )}
      </>
    ) : (
      <span className="readable-scalar">No items</span>
    );
  const entries = Object.entries(data);
  return entries.length ? (
    <>
      <dl className="readable-data">
        {entries.slice(0, 60).map(([key, item]) => (
          <div key={key}>
            <dt>{label(key)}</dt>
            <dd>
              <ReadableData value={item} depth={depth + 1} />
            </dd>
          </div>
        ))}
      </dl>
      {entries.length > 60 && (
        <p className="readable-nested-note">
          Additional fields remain in the original source.
        </p>
      )}
    </>
  ) : (
    <span className="readable-scalar">No details</span>
  );
}

export function CitedProse({
  text,
  citations,
  onEvidence,
}: {
  text: string;
  citations: string[];
  onEvidence: (id: string) => void;
}) {
  const parts: ReactNode[] = text
    .split(/(\[[^\]\n]+\])/g)
    .map((part, index) => {
      const id =
        part.startsWith("[") && part.endsWith("]") ? part.slice(1, -1) : "";
      return id && citations.includes(id) ? (
        <button
          className="summary-citation"
          key={index}
          onClick={() => onEvidence(id)}
          aria-label={`Open cited record ${citations.indexOf(id) + 1}`}
        >
          {citations.indexOf(id) + 1}
        </button>
      ) : (
        part
      );
    });
  return <div className="cited-prose">{parts}</div>;
}
