import { resolveJobFilename } from "./jobUtils";

// Filter rows by a case-insensitive filename substring.  Blank text returns
// the list unchanged.  Single concern: filtering.
export function filterByFilename(rows, text) {
  if (!Array.isArray(rows)) return [];
  const q = String(text || "").trim().toLowerCase();
  if (!q) return rows;
  return rows.filter((row) => resolveJobFilename(row).toLowerCase().includes(q));
}
