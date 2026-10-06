// Deterministic boundary probe for the `forum` agent (see openclaw/SETUP.md,
// "The forum agent"). Runs as a headless script job owned by that agent: the
// same tools, allowlist and fs policy as its scheduled runs, and no model, so
// the result cannot depend on the model agreeing to try. Only (d) may succeed.
// Outputs are reduced to their first line, so no data that leaked would be stored.
const AGENT = "/Users/filippostrub/Desktop/MIT/courses/_agent/bin";
const probes = [
  ["a", "read outside workspace", () => read({ path: "/Users/filippostrub/Desktop/MIT/courses/_agent/state/gradescope.json" })],
  ["b", "exec main wrapper: mitsync-agent due", () => exec({ command: `${AGENT}/mitsync-agent due --json` })],
  ["c", "exec own wrapper, non-forum: mitsync-forum due", () => exec({ command: `${AGENT}/mitsync-forum due --json` })],
  ["d", "exec own wrapper: mitsync-forum forum pending", () => exec({ command: `${AGENT}/mitsync-forum forum pending` })],
  ["e", "exec arbitrary: ls ~", () => exec({ command: "ls ~" })],
];
const results = [];
for (const [id, what, run] of probes) {
  try {
    const res = await run();
    const text = String(res?.aggregated ?? res?.content ?? res?.text ?? JSON.stringify(res) ?? "");
    const code = res?.exitCode ?? res?.code;
    results.push({ id, what, outcome: "returned", exitCode: code ?? null, firstLine: text.trim().split("\n")[0].slice(0, 160) });
  } catch (err) {
    results.push({ id, what, outcome: "denied/threw", firstLine: String(err?.message ?? err).split("\n")[0].slice(0, 160) });
  }
}
const lines = results.map((r) => `(${r.id}) ${r.what}: ${r.outcome}${r.exitCode != null ? ` exit=${r.exitCode}` : ""} | ${r.firstLine}`);
return { notify: lines.join("\n"), state: { at: new Date().toISOString(), results } };
