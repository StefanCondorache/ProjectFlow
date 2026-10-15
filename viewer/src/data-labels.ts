/** Human-readable name for the origin of a data trail. Without identity
 * metadata only Python's "$" temporaries are guessed; "$" is legal JS. */
export function followingLabel(name: string, generated?: boolean | null, language?: string): string {
  return (generated ?? ((!language || language === "python") && name.startsWith("$"))) ? "a call's result" : name;
}

/** Missing metadata preserves rendering for older servers; [] means all user data. */
export function edgeDataLabel(name: string, generated?: string[] | null, result = "result"): string {
  return (generated?.includes(name) ?? name.startsWith("$")) ? result : name;
}
