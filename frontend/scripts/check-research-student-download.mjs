#!/usr/bin/env node
// Regression check (no frontend test framework exists in this repo, so this is a
// plain Node script rather than a new test dependency): the Advisory Committee
// PDF download action was once added only to StaffCommitteeView — the component
// students never see (ResearchPage routes role === "student" to a SEPARATE
// StudentCommitteeView, frontend/app/(app)/research/page.tsx). A naive
// "does the string exist anywhere in the file" check would have missed that
// bug, since the download code DID exist in the file, just in the wrong
// component — so this isolates StudentCommitteeView's own source slice and
// checks inside it specifically.
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const pagePath = path.join(__dirname, "..", "app", "(app)", "research", "page.tsx");
const source = fs.readFileSync(pagePath, "utf8");

const start = source.indexOf("function StudentCommitteeView()");
assert.ok(start !== -1, `StudentCommitteeView function not found (${pagePath})`);
const nextFn = source.indexOf("\nfunction ", start + 1);
assert.ok(nextFn !== -1, `Could not find the end of StudentCommitteeView (${pagePath})`);
const studentView = source.slice(start, nextFn);

assert.ok(
  /\/research\/committees\/\$\{committee\.id\}\/document/.test(studentView),
  "StudentCommitteeView must call the Advisory Committee document download endpoint for the student's OWN committee id",
);
assert.ok(
  studentView.includes("Download Advisory Committee Order"),
  "StudentCommitteeView must render the Advisory Committee download button",
);
assert.ok(
  /committee\.stage\s*===\s*"dpgs_approved"/.test(studentView),
  "the download action must be gated on the committee having reached dpgs_approved",
);

console.log("PASS: the Advisory Committee download action exists inside StudentCommitteeView, gated on dpgs_approved");
