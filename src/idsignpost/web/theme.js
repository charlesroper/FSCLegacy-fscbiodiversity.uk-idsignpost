// Two-state theme logic. The OS preference applies until the visitor chooses the
// other theme. A stored choice exists only while it differs from the OS preference.

export const THEME_KEY = "idsignpost-theme";

export function effectiveTheme(stored, osDark) {
  if (stored === "light" || stored === "dark") return stored;
  return osDark ? "dark" : "light";
}

export function nextOverride(stored, osDark) {
  const next = effectiveTheme(stored, osDark) === "dark" ? "light" : "dark";
  return next === (osDark ? "dark" : "light") ? null : next;
}
