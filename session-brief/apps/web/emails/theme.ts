// Email-safe design tokens for the brief templates. Mirrors design/design-reference.html
// but under the constraints of docs/06: no web fonts (Outlook renders through Word),
// deliberate fallback stacks, and colours that survive dark-mode inversion.

export const palette = {
  paper: "#E4E5E0",
  card: "#F5F5F2",
  ink: "#14161A",
  ink2: "#565C64",
  ink3: "#878D93",
  rule: "#CBCDC6",
  rule2: "#DFE0DB",
  navy: "#1B2A4A",
  ox: "#8C2B25", // oxblood — negative
  pine: "#1F6152", // pine — positive
  mark: "#F5DE63", // highlighter
  white: "#FFFFFF",
} as const;

// Archivo / Newsreader / IBM Plex Mono all fall back — set the stacks so the
// fallback reads as intentional rather than accidental (docs/06).
export const font = {
  disp: '"Helvetica Neue", Arial, sans-serif',
  body: 'Georgia, "Times New Roman", serif',
  mono: 'ui-monospace, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace',
} as const;

// Up = pine, down = oxblood, flat/absent = muted grey.
export function signColor(v: number | null | undefined): string {
  if (v == null || v === 0) return palette.ink3;
  return v > 0 ? palette.pine : palette.ox;
}
