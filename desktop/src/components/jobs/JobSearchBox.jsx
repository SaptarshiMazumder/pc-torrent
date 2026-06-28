// Search input only.  Fully controlled by the parent (value + onChange).
export default function JobSearchBox({ value, onChange, placeholder }) {
  return (
    <div className="jobs-search-box">
      <input
        type="text"
        className="jobs-search-input"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder || "Search by file name…"}
      />
      {value ? (
        <button
          type="button"
          className="jobs-search-clear"
          onClick={() => onChange("")}
          title="Clear"
        >
          ×
        </button>
      ) : null}
    </div>
  );
}
