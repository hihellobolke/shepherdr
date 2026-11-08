// Group a folder's Tasks under their Feature, and build the one summary row each Feature group shows.
// The OpenSpec flow (docs/openspec-flow.md, contract C9): a Feature is a record in the store, not a Task; its
// child Tasks run under it. The window (mods/tasks), the web app and the service all draw the same group row from
// here, so the rule lives in one place.
//
// These are pure functions. They consume records that already carry their ref: the service enriches every Feature
// record with ref (and folder and machine) and every task row with its own ref and feature id before publishing,
// so nothing here computes a ref or reads the folder prefix.
//
// Shared core: the source is core/features.js; bin/sync-shared copies it into the mods in lib/COPIES.

// The phase words the person reads, one per phase of C2 (proposal section 2.3).
export const PHASE_LABEL = {
  planning: "Planning",
  building: "Building",
  verifying: "Verifying",
  closing: "Closing",
  ready: "Ready to merge",
  done: "Done",
  cancelled: "Cancelled",
};

// The two human actions on a Feature group row, and the ids the service actions dispatch on (contract C9).
export const FEATURE_ACTIONS = {
  merge: { id: "feature-merge", label: "Merge feature" },
  fixRound: { id: "feature-fix-round", label: "Add a fix round" },
};

// Merge feature is offered only once the pull request is open and the person decides.
export const mergeReady = (feature) => feature?.phase === "ready";
// Add a fix round is offered only when open findings remain after the allowed fix rounds.
export const canAddFixRound = (feature) => feature?.attention === "verify-open";

// The store key a child's digest lives under in feature.digests (contract C2, digest.child_key): "<machine>:<id>",
// with "local" for the home machine. A child entry carries its id as `ref` (the folder counter integer) and its
// machine (null for home).
const childKey = (child) => `${child.machine ?? "local"}:${child.ref}`;

// A child counts as merged when its digest says so. The digest is written when the child ends (C5); its state is
// one of merged, skipped, cancelled or failed, and only merged is counted in "merged of total".
const isMerged = (feature, child) => (feature.digests ?? {})[childKey(child)]?.state === "merged";

// The open verify findings count: the findings list of the Feature's current Verify Task (C2). The Verify Task is a
// local task row, so it is found among the rows passed in and matched by the Feature's verify_task id, and its
// findings list is counted. A published task row carries no findings list (only the Feature carries the count), so
// when the row has none - or no row is present at all - the record's own findings count stands.
function openFindings(feature, rows) {
  const verify = (rows ?? []).find((t) => t.role === "verify" && t.id === feature.verify_task);
  return Array.isArray(verify?.findings) ? verify.findings.length : feature.findings ?? 0;
}

// The one summary row for a Feature group.
// feature: a ref-enriched Feature record; children: its ref-enriched task rows (local rows of this folder).
// total is the Feature's child count (from the record's children, so remote children on other machines count too);
// merged is how many of those children have merged; findings is the open verify findings count.
// featureRow is idempotent: the service builds the summary from the raw record (which carries children and digests)
// and publishes it on the record; the web may re-invoke this on that already-summarised record (which no longer
// carries children), and then the computed fields pass straight through.
export function featureRow(feature, children) {
  const raw = feature.children != null;
  const actions = [];
  if (mergeReady(feature)) actions.push(FEATURE_ACTIONS.merge);
  if (canAddFixRound(feature)) actions.push(FEATURE_ACTIONS.fixRound);
  return {
    ref: feature.ref,
    title: feature.title,
    phase: feature.phase,
    phaseLabel: PHASE_LABEL[feature.phase] ?? feature.phase,
    merged: raw ? feature.children.filter((c) => isMerged(feature, c)).length : feature.merged ?? 0,
    total: raw ? feature.children.length : feature.total ?? 0,
    findings: openFindings(feature, children),
    actions,
    attention: feature.attention ?? "",
  };
}

// Group a folder's Tasks under their Feature.
// features: ref-enriched Feature records; tasks: ref-enriched task rows (each with a feature id or null).
// Returns one group per Feature, in the given order, each { feature, tasks, row } with row = featureRow(...);
// then, when any remain, a final group of loose Tasks (a classic Task, or a flow Task whose Feature is not here)
// as { feature: null, tasks, row: null }.
export function groupByFeature(features, tasks) {
  const list = features ?? [];
  const byId = new Map(list.map((f) => [f.id, []]));
  const loose = [];
  for (const t of tasks ?? []) {
    const bucket = t.feature != null && byId.has(t.feature) ? byId.get(t.feature) : loose;
    bucket.push(t);
  }
  const groups = list.map((feature) => {
    const rows = byId.get(feature.id);
    return { feature, tasks: rows, row: featureRow(feature, rows) };
  });
  if (loose.length) groups.push({ feature: null, tasks: loose, row: null });
  return groups;
}
