import { useEffect, useState } from "react";

type Direction = "asc" | "desc";
export type SortPreference = readonly [
  string,
  Direction,
  (field: string) => void,
  (direction: Direction) => void,
];
export const sourceSortOptions = [
  ["name", "Name"],
  ["records", "Records"],
  ["files", "Files"],
  ["bytes", "Bytes"],
  ["status", "Status"],
] as const;

export function useSortPreference(
  view: string,
  initial: string,
  fields: readonly string[],
  direction: Direction = "asc",
): SortPreference {
  const key = `archive-sort:${view}`;
  const [[field, order], set] = useState<[string, Direction]>(() => {
    try {
      const value = JSON.parse(localStorage.getItem(key) || "null");
      if (
        Array.isArray(value) &&
        fields.includes(value[0]) &&
        ["asc", "desc"].includes(value[1])
      ) return [value[0], value[1]];
    } catch {
      // Storage can be unavailable.
    }
    return [fields.includes(initial) ? initial : fields[0], direction];
  });
  useEffect(() => {
    try {
      localStorage.setItem(key, JSON.stringify([field, order]));
    } catch {
      // Sorting still works without persistence.
    }
  }, [key, field, order]);
  return [
    field,
    order,
    value => {
      if (fields.includes(value)) set(current => [value, current[1]]);
    },
    value => {
      if (value === "asc" || value === "desc") set(current => [current[0], value]);
    },
  ];
}

export function SortControls({ label, options, value }: {
  label: string;
  options: readonly (readonly [string, string])[];
  value: SortPreference;
}) {
  return (
    <div className="sort-controls">
      <label>
        {label} sort{" "}
        <select
          aria-label={`${label} sort`}
          value={value[0]}
          onChange={event => value[2](event.target.value)}
        >
          {options.map(([field, title]) => <option key={field} value={field}>{title}</option>)}
        </select>
      </label>
      <select
        aria-label={`${label} direction`}
        value={value[1]}
        onChange={event => value[3](event.target.value as Direction)}
      >
        <option value="asc">Ascending</option>
        <option value="desc">Descending</option>
      </select>
    </div>
  );
}

export function sortItems<T>(
  items: readonly T[],
  preference: SortPreference,
  value: (item: T, field: string) => string | number | null | undefined,
): T[] {
  const missing = (item: unknown) =>
    item == null || item === "" || typeof item === "number" && !Number.isFinite(item);
  return [...items].sort((a, b) => {
    const x = value(a, preference[0]), y = value(b, preference[0]);
    if (missing(x) || missing(y)) return Number(missing(x)) - Number(missing(y));
    const result = typeof x === "number" && typeof y === "number"
      ? x - y
      : String(x).localeCompare(String(y), undefined, { numeric: true, sensitivity: "base" });
    return preference[1] === "asc" ? result : -result;
  });
}
