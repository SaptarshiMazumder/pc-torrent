export default function NumberField({ label, value, onChange, step = 1, min, max, hint }) {
  return (
    <label className="cfg-field">
      <span className="cfg-field-label">{label}</span>
      <input
        className="cfg-field-input"
        type="number"
        value={value ?? ""}
        step={step}
        min={min}
        max={max}
        onChange={(e) => {
          const raw = e.target.value;
          if (raw === "") {
            onChange(null);
            return;
          }
          const num = Number(raw);
          if (!Number.isNaN(num)) onChange(num);
        }}
      />
      {hint && <span className="cfg-field-hint">{hint}</span>}
    </label>
  );
}
