import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import {
  groupByFeature,
  featureRow,
  mergeReady,
  canAddFixRound,
  PHASE_LABEL,
  FEATURE_ACTIONS,
} from "./features.js";

const fixture = (name) =>
  JSON.parse(readFileSync(new URL(`../test/fixtures/openspec-flow/${name}`, import.meta.url), "utf8"));
const store = fixture("store.json");
const snapshot = fixture("snapshot.json");

// A store Feature record carries id/machine children and a digests map; the service enriches it with its ref
// before featureRow sees it, so the fixture records (which have no ref) get one here.
const ref = (prefix, id) => `${prefix}-${id}`;
const enrich = (f) => ({ ...f, ref: ref(store.prefix, f.id) });
const loginFlow = enrich(store.features.find((f) => f.id === 12));
const checkoutPolish = enrich(store.features.find((f) => f.id === 18));
const storeRows = (feature) => store.tasks.filter((t) => t.feature === feature.id);

describe("featureRow: the one summary row for a Feature group", () => {
  test("merged of total counts the record's children, remote ones included", () => {
    // Login flow has three children (one on vm-b) and one merged digest.
    const row = featureRow(loginFlow, storeRows(loginFlow));
    expect(row).toMatchObject({ ref: "HAT-12", title: "Login flow", total: 3, merged: 1 });
  });
  test("the phase and its human label come straight from the record", () => {
    expect(featureRow(loginFlow, storeRows(loginFlow))).toMatchObject({
      phase: "building",
      phaseLabel: "Building",
      attention: "",
    });
  });
  test("a Feature with no children is zero of zero", () => {
    const row = featureRow(checkoutPolish, storeRows(checkoutPolish));
    expect(row).toMatchObject({ total: 0, merged: 0, findings: 0 });
  });
  test("only a merged digest counts; a skipped or failed one does not", () => {
    const feature = {
      ...loginFlow,
      children: [
        { ref: 14, machine: null },
        { ref: 15, machine: "vm-b" },
        { ref: 16, machine: null },
      ],
      digests: {
        "local:14": { state: "merged" },
        "vm-b:15": { state: "skipped" },
        "local:16": { state: "failed" },
      },
    };
    expect(featureRow(feature, []).merged).toBe(1);
    expect(featureRow(feature, []).total).toBe(3);
  });
  test("open findings come from the current Verify Task's findings list", () => {
    const rows = [
      { id: 17, role: "verify", findings: [{ level: "blocker" }, { level: "minor" }] },
      { id: 14, role: "child" },
    ];
    const feature = { ...loginFlow, verify_task: 17 };
    expect(featureRow(feature, rows).findings).toBe(2);
  });
  test("findings from an older Verify Task are not counted, only the current one", () => {
    const rows = [
      { id: 17, role: "verify", findings: [{ level: "major" }] },
      { id: 30, role: "verify", findings: [] },
    ];
    expect(featureRow({ ...loginFlow, verify_task: 30 }, rows).findings).toBe(0);
  });
  test("no Verify Task yet means no findings", () => {
    expect(featureRow(checkoutPolish, storeRows(checkoutPolish)).findings).toBe(0);
  });
  test("idempotent: an already-summarised record (no children) passes its counts through", () => {
    // The web re-groups the service's published features, which carry merged/total/findings but no raw children.
    const published = snapshot.features.find((f) => f.ref === "HAT-40");
    const row = featureRow(published, []);
    expect(row).toMatchObject({ ref: "HAT-40", merged: 6, total: 6, findings: 2, phaseLabel: "Verifying" });
  });
  test("a published Verify Task row without a findings list keeps the record's findings count", () => {
    // Published task rows carry no findings list; only the Feature carries the count. Re-grouping must not zero it.
    const published = snapshot.features.find((f) => f.ref === "HAT-40");
    const verifyRow = { id: published.verify_task, feature: 40, role: "verify" };
    expect(featureRow(published, [verifyRow]).findings).toBe(2);
  });
  test("a Verify Task row that does carry a findings list is counted from it", () => {
    const published = snapshot.features.find((f) => f.ref === "HAT-40");
    const verifyRow = { id: published.verify_task, feature: 40, role: "verify", findings: [{ level: "blocker" }] };
    expect(featureRow(published, [verifyRow]).findings).toBe(1);
  });
});

describe("featureRow: the two actions, and when each is offered", () => {
  test("building offers neither action", () => {
    expect(featureRow(loginFlow, storeRows(loginFlow)).actions).toEqual([]);
  });
  test("ready offers Merge feature only", () => {
    const row = featureRow({ ...checkoutPolish, phase: "ready" }, []);
    expect(row.actions).toEqual([FEATURE_ACTIONS.merge]);
  });
  test("open findings offer Add a fix round only", () => {
    const row = featureRow({ ...loginFlow, phase: "verifying", attention: "verify-open" }, []);
    expect(row.actions).toEqual([FEATURE_ACTIONS.fixRound]);
  });
  test("the action ids match the service action types", () => {
    expect(FEATURE_ACTIONS.merge.id).toBe("feature-merge");
    expect(FEATURE_ACTIONS.fixRound.id).toBe("feature-fix-round");
  });
});

describe("mergeReady and canAddFixRound", () => {
  test("mergeReady is true only in phase ready", () => {
    expect(mergeReady({ phase: "ready" })).toBe(true);
    for (const phase of ["planning", "building", "verifying", "closing", "done", "cancelled"])
      expect(mergeReady({ phase })).toBe(false);
  });
  test("canAddFixRound is true only when attention is verify-open", () => {
    expect(canAddFixRound({ attention: "verify-open" })).toBe(true);
    expect(canAddFixRound({ attention: "" })).toBe(false);
    expect(canAddFixRound({ attention: "child-failed" })).toBe(false);
  });
  test("a missing feature is safe", () => {
    expect(mergeReady(undefined)).toBe(false);
    expect(canAddFixRound(null)).toBe(false);
  });
});

describe("PHASE_LABEL covers every phase", () => {
  test("the seven phases each have a word", () => {
    for (const phase of ["planning", "building", "verifying", "closing", "ready", "done", "cancelled"])
      expect(typeof PHASE_LABEL[phase]).toBe("string");
  });
  test("an unknown phase falls back to its own name", () => {
    expect(featureRow({ phase: "weird", children: [] }, []).phaseLabel).toBe("weird");
  });
});

describe("groupByFeature: Tasks nested under their Feature", () => {
  const features = snapshot.features;
  const tasks = snapshot.tasks;

  test("one group per Feature, in the given order, then the loose group", () => {
    const groups = groupByFeature(features, tasks);
    expect(groups.map((g) => g.feature?.ref ?? "loose")).toEqual(["HAT-12", "HAT-18", "HAT-40", "loose"]);
  });
  test("a Feature's Tasks land under it, matched by feature id", () => {
    const groups = groupByFeature(features, tasks);
    const login = groups.find((g) => g.feature?.ref === "HAT-12");
    expect(login.tasks.map((t) => t.ref)).toEqual(["HAT-13", "HAT-14", "HAT-15", "HAT-16", "HAT-17"]);
  });
  test("a classic Task with no feature is loose, under feature null with a null row", () => {
    const groups = groupByFeature(features, tasks);
    const loose = groups.find((g) => g.feature === null);
    expect(loose.row).toBe(null);
    expect(loose.tasks.map((t) => t.ref)).toEqual(["HAT-7"]);
  });
  test("each Feature group carries its summary row", () => {
    const groups = groupByFeature(features, tasks);
    const login = groups.find((g) => g.feature?.ref === "HAT-12");
    expect(login.row).toMatchObject({ ref: "HAT-12", total: 3 });
  });
  test("a Feature with no local Tasks still appears as a group", () => {
    const groups = groupByFeature(features, tasks);
    const checkout = groups.find((g) => g.feature?.ref === "HAT-18");
    expect(checkout).toBeDefined();
    expect(checkout.tasks).toEqual([]);
  });
  test("a flow Task whose Feature is not in the list is loose, not dropped", () => {
    const orphan = { ref: "HAT-99", id: 99, feature: 777 };
    const groups = groupByFeature(features, [...tasks, orphan]);
    const loose = groups.find((g) => g.feature === null);
    expect(loose.tasks.map((t) => t.ref)).toContain("HAT-99");
  });
  test("no loose group when every Task belongs to a Feature", () => {
    const onlyFlow = tasks.filter((t) => t.feature != null);
    const groups = groupByFeature(features, onlyFlow);
    expect(groups.some((g) => g.feature === null)).toBe(false);
  });
  test("empty inputs give no groups", () => {
    expect(groupByFeature([], [])).toEqual([]);
    expect(groupByFeature(undefined, undefined)).toEqual([]);
  });
});
