import { useState } from "react";
import { sortRows, rowComparator } from "./jobSort";
import { filterByFilename } from "./jobSearch";

// Owns one table section's SORT state and derives its displayed rows.
// Search text is passed in (shared across sections by the page); the column
// set is passed in too, so this hook never imports the (JSX) column module
// -- it stays pure logic.  Default sort: newest submitted first.
export function useJobTableView(initial = { key: "submitted", dir: "desc" }) {
  const [sort, setSort] = useState(initial);

  const toggleSort = (key) => {
    setSort((prev) =>
      prev.key === key
        ? { key, dir: prev.dir === "asc" ? "desc" : "asc" }
        : { key, dir: "desc" }
    );
  };

  const deriveRows = (jobs, search, columns) => {
    const filtered = filterByFilename(jobs, search);
    const col = Array.isArray(columns) ? columns.find((c) => c.key === sort.key) : null;
    return col ? sortRows(filtered, rowComparator(col, sort.dir)) : filtered;
  };

  return { sort, toggleSort, deriveRows };
}
