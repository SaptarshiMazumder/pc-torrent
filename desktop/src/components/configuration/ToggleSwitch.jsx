export default function ToggleSwitch({ label, value, onChange, hint }) {
  const on = !!value;
  return (
    <label className="cfg-field cfg-field-toggle">
      <span className="cfg-field-label">{label}</span>
      <button
        type="button"
        className={`cfg-toggle${on ? " on" : ""}`}
        role="switch"
        aria-checked={on}
        onClick={() => onChange(!on)}
      >
        <span className="cfg-toggle-thumb" />
      </button>
      {hint && <span className="cfg-field-hint">{hint}</span>}
    </label>
  );
}
