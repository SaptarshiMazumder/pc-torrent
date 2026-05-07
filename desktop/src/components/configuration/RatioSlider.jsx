export default function RatioSlider({
  label,
  value,
  onChange,
  min = 0,
  max = 1,
  step = 0.01,
  precision = 2,
  hint,
}) {
  const v = typeof value === "number" ? value : 0;
  return (
    <label className="cfg-field cfg-field-slider">
      <div className="cfg-field-slider-head">
        <span className="cfg-field-label">{label}</span>
        <span className="cfg-field-slider-value">{v.toFixed(precision)}</span>
      </div>
      <input
        className="cfg-field-range"
        type="range"
        min={min}
        max={max}
        step={step}
        value={v}
        onChange={(e) => onChange(Number(e.target.value))}
      />
      {hint && <span className="cfg-field-hint">{hint}</span>}
    </label>
  );
}
