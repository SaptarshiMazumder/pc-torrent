/**
 * Format a credit-denominated number for display.  Returns "—" for
 * null / undefined / NaN so callers don't have to special-case.
 */
export function formatCredits(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  // Whole credits look cleaner; sub-credit values keep one decimal so a
  // freshly-started chunk doesn't read "0 credits".
  if (Math.abs(value) >= 10) return value.toFixed(0);
  return value.toFixed(1);
}
