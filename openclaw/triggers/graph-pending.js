// Condition gate for the `graph-build` automation (see openclaw/SETUP.md).
// Runs after each sync and after filing has had its turn. It refreshes the
// graph's deterministic half again (filing may have added files since the
// sync), then fires the mit-graph-build agent only when `graph check` lists an
// `error` item the previous check had not seen. An item the agent could not
// close does not fire it again, and `human` items never do: they wait for the
// student. A failed command throws, so it shows up as an errored run.
const M = "/Users/filippostrub/Desktop/MIT/courses/_agent/bin/mitsync-agent";
await exec({ command: `${M} graph refresh` });
const res = await exec({ command: `${M} graph check --json --exit-zero` });
const out = String(res?.aggregated ?? "");
const doc = JSON.parse(out.slice(out.indexOf("{")));
if (!Array.isArray(doc.violations)) throw new Error(`graph check gave no violations: ${out.slice(0, 200)}`);
const open = doc.violations.filter((v) => v.severity === "error").map((v) => `${v.code}:${v.node}`);
const seen = new Set(trigger.state?.open ?? []);
const fresh = open.filter((k) => !seen.has(k));
json({
  fire: fresh.length > 0,
  message: `${fresh.length} new graph check items (${open.length} open, ${doc.counts.human} for the student).`,
  state: { open },
});
