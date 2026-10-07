// Condition gate for the `canvas-file` automation (see openclaw/SETUP.md).
// Fires the filing agent while an unfiled Canvas file in a mapped course has
// not been filed, skipped or left out by a plan. The agent's plan is the
// memory: a file it left out (`left_out` in the plan) never fires it again, so
// the model is paid only for undecided material, and a run that failed before
// applying its plan is retried at the next check instead of forgotten.
// While a sync holds the manifest, `unfiled --ids` answers {"busy": true}: no
// fire, state kept, try again next check. The same undecided files firing
// MAX_FIRES checks in a row means the agent keeps failing on them, so the
// check throws and every later check errors until someone looks.
// A failed check throws, so it shows up as an errored run instead of going quiet.
const MAX_FIRES = 3;
const res = await exec({
  command: "/Users/filippostrub/Desktop/MIT/courses/_agent/bin/mitsync-agent unfiled --ids",
});
const out = String(res?.aggregated ?? "");
const doc = JSON.parse(out.slice(out.indexOf("{")));
if (doc.busy === true) {
  json({
    fire: false,
    message: "A sync holds the manifest; checking again next time.",
    state: trigger.state ?? {},
  });
} else {
  const ids = doc.file_ids;
  if (!Array.isArray(ids)) throw new Error(`unfiled --ids gave no file_ids: ${out.slice(0, 200)}`);
  const same = ids.length > 0 && JSON.stringify(ids) === JSON.stringify(trigger.state?.ids ?? []);
  const fires = ids.length === 0 ? 0 : same ? (trigger.state?.fires ?? 0) + 1 : 1;
  if (fires > MAX_FIRES) {
    throw new Error(
      `${MAX_FIRES} filing runs left ${ids.join(", ")} undecided (not filed, skipped or left out). ` +
        "Read their run history; file them by hand to clear this.",
    );
  }
  json({
    fire: ids.length > 0,
    message: `${ids.length} undecided Canvas files to file (filing run ${fires} of ${MAX_FIRES} for them).`,
    state: { ids, fires },
  });
}
