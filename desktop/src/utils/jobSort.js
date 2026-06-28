// Pure, generic row sort -- field-agnostic.  Stable: ties keep their
// original relative order.

function compareValues(a, b) {
  if (typeof a === "string" || typeof b === "string") {
    return String(a ?? "").localeCompare(String(b ?? ""));
  }
  const an = Number(a) || 0;
  const bn = Number(b) || 0;
  if (an < bn) return -1;
  if (an > bn) return 1;
  return 0;
}

// Resolve a row comparator for a column + direction.  A column may supply
// its own `comparator(a, b, dir)` for ordering the generic value compare
// can't express (e.g. Cost pins in-progress rows to the top and null-cost
// rows to the bottom).  Otherwise compare `sortValue` with direction applied.
export function rowComparator(column, dir) {
  if (typeof column.comparator === "function") {
    return (a, b) => column.comparator(a, b, dir);
  }
  const factor = dir === "asc" ? 1 : -1;
  return (a, b) => compareValues(column.sortValue(a), column.sortValue(b)) * factor;
}

export function sortRows(rows, comparator) {
  if (!Array.isArray(rows) || typeof comparator !== "function") return rows || [];
  return rows
    .map((row, index) => ({ row, index }))
    .sort((a, b) => {
      const cmp = comparator(a.row, b.row);
      return cmp !== 0 ? cmp : a.index - b.index;
    })
    .map((w) => w.row);
}
