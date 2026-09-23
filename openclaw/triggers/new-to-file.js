// Condition gate for the `canvas-file` automation (see openclaw/SETUP.md).
// Fires the filing agent only when an unfiled Canvas file in a mapped course
// appears that the previous check had not seen. Leftovers the agent chose not
// to file never fire it again, so the model is paid only for new material.
// A failed check throws, so it shows up as an errored run instead of going quiet.
const res = await exec({
  command: "/Users/filippostrub/Desktop/MIT/courses/_agent/bin/mitsync-agent unfiled --ids",
});
const out = String(res?.aggregated ?? "");
const ids = JSON.parse(out.slice(out.indexOf("{"))).file_ids;
if (!Array.isArray(ids)) throw new Error(`unfiled --ids gave no file_ids: ${out.slice(0, 200)}`);
const seen = new Set(trigger.state?.ids ?? []);
const fresh = ids.filter((id) => !seen.has(id));
json({
  fire: fresh.length > 0,
  message: `${fresh.length} new Canvas files to file (${ids.length} unfiled in all).`,
  state: { ids },
});
