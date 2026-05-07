export default function TextField({ label, value, onChange, placeholder, hint }) {
  return (
    <label className="cfg-field">
      <span className="cfg-field-label">{label}</span>
      <input
        className="cfg-field-input"
        type="text"
        value={value ?? ""}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
      />
      {hint && <span className="cfg-field-hint">{hint}</span>}
    </label>
  );
}
