// Which list item a 422 is about. The API layer files an item's error under the list's
// name (`keywords`) and its dotted path (`keywords.1`); the dialog names the item by
// quoting it as it split the input, and keeps agentd's message verbatim.

/** "a, b,, c" -> ["a", "b", "c"] */
export function splitList(text: string): string[] {
  return text
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

/** `keywords.3` -> 3 for the list `keywords` (nothing for other keys). */
function itemIndex(key: string, name: string): number | undefined {
  const m = key.startsWith(`${name}.`) ? /^(\d+)$/.exec(key.slice(name.length + 1)) : null;
  return m ? Number(m[1]) : undefined;
}

/** The lines to show under a list input: one per offending item, quoted
 *  (`“(evil)”: String should match pattern …`), plus the list's own message when it
 *  isn't one of theirs (e.g. too many items). */
export function listErrors(fields: Record<string, string>, name: string, items: string[]): string[] {
  const found = Object.keys(fields)
    .map((key) => ({ key, index: itemIndex(key, name) }))
    .filter((k): k is { key: string; index: number } => k.index !== undefined)
    .sort((a, b) => a.index - b.index);
  const lines = found.map(({ key, index }) =>
    index < items.length ? `“${items[index]}”: ${fields[key]}` : fields[key],
  );
  const own = fields[name];
  if (own !== undefined && !found.some(({ key }) => fields[key] === own)) lines.unshift(own);
  return lines;
}

/** The 422 keys a list input shows: its name and every `name.N`, for `formError`. */
export function listKeys(fields: Record<string, string>, name: string): string[] {
  return [name, ...Object.keys(fields).filter((key) => itemIndex(key, name) !== undefined)];
}
