// Condition gate for the `canvas-forum` automation (see openclaw/SETUP.md).
// Wakes the forum agent only when the Homework 3 forum has entries by others
// that it has not seen yet, the course team's control line reads RUNNING, and
// the agent has not stopped after repeated failures. `forum pending` also
// settles a post an interrupted run sent but never logged.
// A failed check throws, so it shows up as an errored run instead of going quiet.
const res = await exec({
  command: "/Users/filippostrub/Desktop/MIT/courses/_agent/bin/mitsync-forum forum pending",
});
const out = String(res?.aggregated ?? "");
const doc = JSON.parse(out.slice(out.indexOf("{")));
if (typeof doc.fire !== "boolean") throw new Error(`forum pending gave no fire: ${out.slice(0, 200)}`);
json({
  fire: doc.fire,
  message: `${doc.new} unseen forum entries (${doc.replies_to_me} replies to you); control line ${doc.control}.`,
  state: { newest: doc.newest, control: doc.control, stopped: doc.stopped },
});
