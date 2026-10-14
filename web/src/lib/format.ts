/* Formatting only. Nothing here invents a number: every value handed in came
   from run_report.json or a layer file the API served. */

export function num(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return value.toLocaleString("en-GB", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

/** Integers plain, fractions to one place — used for hectares and kilometres. */
export function measure(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return num(value, Number.isInteger(value) ? 0 : 1);
}

export function percent(fraction: number | null | undefined, digits = 1): string {
  if (fraction === null || fraction === undefined || !Number.isFinite(fraction)) return "—";
  return `${num(fraction * 100, digits)}%`;
}

const MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

/** "2026-08-26" -> "26 August 2026". Falls back to the input it was given. */
export function longDate(iso: string | undefined | null): string {
  if (!iso) return "—";
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  if (!match) return iso;
  const [, year, month, day] = match;
  return `${Number(day)} ${MONTHS[Number(month) - 1]} ${year}`;
}

/** The area, named the only honest way: by its own coordinates. */
export function bboxLabel(bbox: number[] | undefined): string {
  if (!bbox || bbox.length !== 4) return "No area selected";
  const [w, s, e, n] = bbox;
  const lon = `${w.toFixed(2)}–${e.toFixed(2)} E`;
  const lat = `${s.toFixed(2)}–${n.toFixed(2)} N`;
  return `${lon}, ${lat}`;
}

export function bboxSpanKm(bbox: number[] | undefined): string {
  if (!bbox || bbox.length !== 4) return "—";
  const [w, s, e, n] = bbox;
  const midLat = ((s + n) / 2) * (Math.PI / 180);
  const kmX = (e - w) * 111.32 * Math.cos(midLat);
  const kmY = (n - s) * 110.57;
  return `${num(kmX, 0)} by ${num(kmY, 0)} km`;
}

/** A run id as a person would read it. */
export function runLabel(runId: string): string {
  return runId.replace(/_/g, " ");
}
