// One header cell.  Shows the column label; when this column is the active
// sort it shows an up/down arrow.  Clicking a sortable header calls
// onSort(key) (the parent decides asc/desc toggling).
export default function SortableHeaderCell({ column, sort, onSort }) {
  const active = sort?.key === column.key;
  const arrow = !active ? "" : sort.dir === "asc" ? "▲" : "▼";
  const cls =
    `job-th align-${column.align || "left"}` +
    (column.sortable ? " sortable" : "") +
    (active ? " active" : "");

  if (!column.sortable) {
    return <th className={cls}>{column.label}</th>;
  }
  return (
    <th
      className={cls}
      role="button"
      tabIndex={0}
      onClick={() => onSort(column.key)}
      onKeyDown={(e) => e.key === "Enter" && onSort(column.key)}
    >
      <span className="job-th-label">{column.label}</span>
      <span className="job-th-arrow">{arrow}</span>
    </th>
  );
}
