// SHMD-16 (OpenSpec flow, T21c): the service carries a folder's Features into the snapshot (service/model.ts
// featuresOf, wired through build() and service/compat.ts), and runs the two Feature actions through herdr-task
// (service/actions.ts). These tests use the real shared featureRow (core/features.js) and the store fixture of C10, a
// real model build for the row wiring, and a fake herdr-task (SHEPHERDR_HERDR_TASK) for the actions.
import { afterAll, beforeAll, expect, test } from "bun:test";
import { chmodSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { featuresOf } from "./model.ts";
import { Compat } from "./compat.ts";
import { act } from "./actions.ts";

const ROOT = join(import.meta.dir, "..");
const store = JSON.parse(readFileSync(join(ROOT, "test", "fixtures", "openspec-flow", "store.json"), "utf8"));

// the list the model builds for a folder (core/tasklists.js taskLists + the model's enrichment) and its ref-enriched
// task rows (only the fields build() carries), the classic task among them
const list = { ...store, machine: null };
const rows = store.tasks.map((t: any) => ({
  ref: `${store.prefix}-${t.id}`, id: t.id, feature: t.feature, folder: store.folder, machine: null, section: t.state,
  ...(t.role ? { role: t.role } : {}), ...(t.risk ? { risk: t.risk } : {}), ...(Array.isArray(t.findings) ? { findings: t.findings } : {}),
}));

test("featuresOf enriches each store Feature with ref/folder/machine and the real featureRow summary", () => {
  const features = featuresOf([list], rows);

  // the two Features of the fixture, in store order; the classic task (no feature) makes none
  expect(features.map((f: any) => f.ref)).toEqual(["HAT-12", "HAT-18"]);

  const login = features[0];
  expect(login.id).toBe(12);
  expect(login.ref).toBe("HAT-12");
  expect(login.folder).toBe("/tmp/throwaway/shop");
  expect(login.machine).toBeNull();
  expect(login.change).toBe("add-login-flow"); // a record field comes through
  // the real featureRow summary: total/merged from the record's children + digests, findings from the Verify row
  expect(login).toMatchObject({ phase: "building", phaseLabel: "Building", total: 3, merged: 1, findings: 0, actions: [] });
  // the heavy record fields are left out of the snapshot entry
  expect("children" in login).toBe(false);
  expect("digests" in login).toBe(false);
  expect("created" in login).toBe(false);

  // the planning Feature has no children yet
  expect(features[1]).toMatchObject({ ref: "HAT-18", phaseLabel: "Planning", total: 0, merged: 0, findings: 0, actions: [] });
});

test("featuresOf counts a Feature's open findings from its Verify row and offers the right action", () => {
  const verifying = {
    folder: "/r", prefix: "HAT", machine: null,
    features: [{ id: 40, title: "Search revamp", phase: "verifying", attention: "verify-open", verify_task: 55, fix_rounds: 2,
      children: [{ ref: 50, machine: null }, { ref: 51, machine: "vm-b" }],
      digests: { "local:50": { state: "merged" }, "vm-b:51": { state: "merged" } } }],
    tasks: [],
  };
  const vrows = [{ ref: "HAT-55", id: 55, feature: 40, folder: "/r", role: "verify", findings: [{ level: "major", title: "x" }, { level: "minor", title: "y" }] }];
  const [f] = featuresOf([verifying], vrows);
  expect(f).toMatchObject({ ref: "HAT-40", total: 2, merged: 2, findings: 2 }); // merged counts the remote child too
  expect(f.actions).toEqual([{ id: "feature-fix-round", label: "Add a fix round" }]);
});

test("two machines' Features with the same id and folder path keep their own Verify findings (no leak)", () => {
  // each machine's store has its own counter, so a Feature id and folder path can collide across machines (C2); the
  // findings must come from each Feature's own store's Verify row, not the first row that matches id + folder
  const feature = (title: string) => ({ id: 40, title, phase: "verifying", attention: "", verify_task: 55, children: [], digests: {} });
  const localList = { folder: "/repo", prefix: "HAT", machine: null, features: [feature("Search here")], tasks: [] };
  const vmbList = { folder: "/repo", prefix: "HAT", machine: "vm-b", features: [feature("Search on vm-b")], tasks: [] };
  const rows = [
    { ref: "HAT-55", id: 55, feature: 40, folder: "/repo", machine: null, role: "verify", findings: [] },
    { ref: "HAT-55", id: 55, feature: 40, folder: "/repo", machine: "vm-b", role: "verify", findings: [{ level: "blocker", title: "leak" }] },
  ];
  const out = featuresOf([localList, vmbList], rows);
  expect(out.map((f: any) => [f.machine, f.findings])).toEqual([[null, 0], ["vm-b", 1]]);
});

test("a Feature ready to merge offers Merge feature", () => {
  const ready = { folder: "/r", prefix: "HAT", machine: null, features: [{ id: 41, title: "Done thing", phase: "ready", attention: "", children: [], digests: {} }], tasks: [] };
  const [f] = featuresOf([ready], []);
  expect(f.actions).toEqual([{ id: "feature-merge", label: "Merge feature" }]);
});

test("a folder with no Features (flow=classic) yields no Feature entries", () => {
  expect(featuresOf([{ ...store, features: undefined }], rows)).toEqual([]);
});

test("the model's build carries each flow Task's feature into its row and computes the Features", async () => {
  const out = `shop.json\n${JSON.stringify(store)}\n@@herdr-task-list@@\n`;
  const hub: any = {
    terminals: new Map(),
    on: () => () => {},
    async call(method: string) {
      if (method === "machine.exec") return { code: 0, stdout: Buffer.from(out).toString("base64"), stderr: "" };
      if (method === "doc.get") return { value: { spaces: [] } };
      if (method === "machine.list") return { machines: [] };
      throw new Error(method);
    },
  };
  const { Model } = await import("./model.ts");
  const m = new Model(hub, { put: () => {} } as any);
  let built: any = null;
  m.onBuild = (b) => { built = b; };
  await (m as any).readLists(null);
  (m as any).build();
  const row = (ref: string) => built.rows.find((r: any) => r.ref === ref);
  expect(row("HAT-14").feature).toBe(12);
  expect(row("HAT-14").role).toBe("child");
  expect(row("HAT-17").role).toBe("verify");
  expect("feature" in row("HAT-7")).toBe(false); // the classic task keeps no flow fields
  expect(built.features.map((f: any) => f.ref)).toEqual(["HAT-12", "HAT-18"]);
  expect(built.features.find((f: any) => f.ref === "HAT-12")).toMatchObject({ total: 3, merged: 1, findings: 0, phaseLabel: "Building" });
});

test("compat.snapshot carries the Features, and a flow Task keeps its feature id; a classic one does not", () => {
  const features = featuresOf([list], rows);
  const compat = new Compat() as any;
  const webRows = [
    { ref: "HAT-15", id: 15, title: "Parse body", section: "running", machine: null, folder: store.folder, feature: 12, role: "child", risk: "high" },
    { ref: "HAT-7", id: 7, title: "Fix a typo", section: "ready", machine: null, folder: store.folder },
  ];
  const snap = compat.snapshot({ host: "vm", user: "You", machines: [], spaces: [], sections: [], rows: webRows, features });
  expect(snap.features.map((f: any) => f.ref)).toEqual(["HAT-12", "HAT-18"]);
  const child = snap.tasks.find((t: any) => t.id === "HAT-15");
  expect(child).toMatchObject({ feature: 12, role: "child", risk: "high" }); // membership reaches the web (groupByFeature)
  expect("feature" in snap.tasks.find((t: any) => t.id === "HAT-7")).toBe(false);

  // no Features given: the field is still there, empty (a classic install, or an older snapshot caller)
  expect(compat.snapshot({ host: "vm", user: "You", machines: [], spaces: [], sections: [], rows: [] }).features).toEqual([]);
});

// a fake herdr-task: it records its argv next to itself and answers the two Feature commands; HAT-99 refuses with a
// JSON error, HAT-500 fails without one (stderr only). Bun.spawn gives the child the parent's env snapshot (not live
// process.env), so the calls path can't be handed over by an env var set in the test.
let dir: string;
let calls: string;
beforeAll(() => {
  dir = mkdtempSync(join(tmpdir(), "shmd16-"));
  calls = join(dir, "calls.txt");
  const fake = join(dir, "herdr-task");
  writeFileSync(fake, [
    "#!/bin/sh",
    'printf "%s\\n" "$*" >> "$(dirname "$0")/calls.txt"',
    'if [ "$1" = "--on" ]; then shift 2; fi',
    'ref="$3"',
    'if [ "$ref" = "HAT-99" ]; then printf \'{"error":"feature %s is not ready to merge"}\\n\' "$ref"; exit 1; fi',
    'if [ "$ref" = "HAT-500" ]; then echo "herdr-task: it fell over" >&2; exit 1; fi',
    'case "$2" in',
    '  merge) printf \'{"ok":true,"pr":"https://example.test/pr/1","phase":"done"}\\n\';;',
    '  fix-round) printf \'{"ok":true,"fix_rounds":2,"verify_task":"HAT-32"}\\n\';;',
    '  *) printf \'{"error":"unexpected %s"}\\n\' "$2"; exit 1;;',
    "esac",
  ].join("\n") + "\n");
  chmodSync(fake, 0o755);
  process.env.SHEPHERDR_HERDR_TASK = fake;
});
afterAll(() => {
  delete process.env.SHEPHERDR_HERDR_TASK;
  rmSync(dir, { recursive: true, force: true });
});

const hub: any = {};
const compat: any = {};

test("feature-merge runs `herdr-task feature merge <ref>` and returns ok", async () => {
  writeFileSync(calls, "");
  const out = await act(hub, { type: "feature-merge", feature: "HAT-12", folder: "/tmp/throwaway/shop", machine: null }, compat);
  expect(out).toEqual({ ok: true });
  expect(readFileSync(calls, "utf8").trim()).toBe("feature merge HAT-12 --folder /tmp/throwaway/shop --json");
});

test("feature-fix-round runs `herdr-task feature fix-round <ref>` and returns ok", async () => {
  writeFileSync(calls, "");
  const out = await act(hub, { type: "feature-fix-round", feature: "HAT-12", folder: "/tmp/throwaway/shop", machine: null }, compat);
  expect(out).toEqual({ ok: true });
  expect(readFileSync(calls, "utf8").trim()).toBe("feature fix-round HAT-12 --folder /tmp/throwaway/shop --json");
});

test("a Feature action on another machine passes --on <machine>", async () => {
  writeFileSync(calls, "");
  await act(hub, { type: "feature-merge", feature: "HAT-12", folder: "/tmp/throwaway/shop", machine: "vm-b" }, compat);
  expect(readFileSync(calls, "utf8").trim()).toBe("--on vm-b feature merge HAT-12 --folder /tmp/throwaway/shop --json");
});

test("a refused Feature action throws the command's JSON error", async () => {
  await expect(act(hub, { type: "feature-merge", feature: "HAT-99", folder: "/tmp/throwaway/shop", machine: null }, compat))
    .rejects.toThrow("feature HAT-99 is not ready to merge");
});

test("a Feature action that fails without JSON throws its stderr line", async () => {
  await expect(act(hub, { type: "feature-fix-round", feature: "HAT-500", folder: "/tmp/throwaway/shop", machine: null }, compat))
    .rejects.toThrow("herdr-task: it fell over");
});

test("a bad Feature ref is refused before any command runs", async () => {
  writeFileSync(calls, "");
  await expect(act(hub, { type: "feature-merge", feature: "not a ref", folder: "/tmp/throwaway/shop", machine: null }, compat))
    .rejects.toThrow("bad feature");
  expect(readFileSync(calls, "utf8").trim()).toBe("");
});
