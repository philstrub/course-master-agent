// Condition gate for the `course-notes` automation (see openclaw/SETUP.md).
// Extracts text from new documents, rebuilds the backbone, and fires the
// mit-course agent only when `kb check` lists a document (or a readings
// problem) the previous check had not seen. What the agent left pending on
// purpose (an unreadable scan) never fires it again.
// A failed check throws, so it shows up as an errored run instead of going quiet.
const M = "/Users/filippostrub/Desktop/MIT/courses/_agent/bin/mitsync-agent";
await exec({ command: `${M} extract` });
await exec({ command: `${M} kb build` });
const res = await exec({ command: `${M} kb check --json --exit-zero` });
const out = String(res?.aggregated ?? "");
const doc = JSON.parse(out.slice(out.indexOf("{")));
if (!Array.isArray(doc.courses)) throw new Error(`kb check gave no courses: ${out.slice(0, 200)}`);
const keys = [
  ...doc.courses.flatMap((c) => [
    ...(c.exists ? [] : [`missing:${c.course}`]),
    ...c.pending.map((d) => `${d.path}:${d.sha256}`),
    ...c.gone.map((p) => `gone:${p}`),
  ]),
  ...doc.readings.map((r) => `${r.code}:${r.message}`),
];
const seen = new Set(trigger.state?.keys ?? []);
const fresh = keys.filter((k) => !seen.has(k));
json({
  fire: fresh.length > 0,
  message: `${fresh.length} new items for the course master files (${keys.length} open in all).`,
  state: { keys },
});
