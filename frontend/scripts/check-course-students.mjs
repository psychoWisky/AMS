#!/usr/bin/env node
// Regression check (no frontend test framework exists in this repo, so this is a
// plain Node script rather than a new test dependency, following the same
// convention as check-ppw-preview-address.mjs / check-research-student-download.mjs):
// the Course Students feature must stay navigable by exactly super_admin, hod,
// the 4 scoped Incharge Academic Cell roles (Incharge Academic Cell
// scope-split task, this revision — superseded the single
// `incharge_academic_cell` role) and dpgs, and the page must call the real
// backend endpoints rather than a stub.
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const navPath = path.join(__dirname, "..", "lib", "navigation.ts");
const navSource = fs.readFileSync(navPath, "utf8");
const navLine = navSource.split("\n").find((l) => l.includes('href: "/course-students"'));
assert.ok(navLine, `navigation.ts must have a NAV entry for /course-students (${navPath})`);
for (const role of ["super_admin", "hod", "dpgs"]) {
  assert.ok(navLine.includes(`"${role}"`), `Course Students nav entry must include role "${role}": ${navLine}`);
}
// Incharge Academic Cell scope-split task (this revision) — the nav entry
// now spreads the shared INCHARGE_ROLES list (lib/utils.ts) rather than a
// literal "incharge_academic_cell" string.
assert.ok(navLine.includes("...INCHARGE_ROLES"), `Course Students nav entry must include ...INCHARGE_ROLES: ${navLine}`);
assert.ok(!navLine.includes('"faculty"') && !navLine.includes('"student"'),
  `Course Students nav entry must NOT include "faculty" or "student": ${navLine}`);

const pagePath = path.join(__dirname, "..", "app", "(app)", "course-students", "page.tsx");
assert.ok(fs.existsSync(pagePath), `Course Students page must exist at ${pagePath}`);
const pageSource = fs.readFileSync(pagePath, "utf8");
assert.ok(pageSource.includes('"/courses/offerings/all"'), "page must call the real course-offerings endpoint, not a stub");
assert.ok(/\/enrollment\/offering\/\$\{offering\.id\}/.test(pageSource), "page must call the real per-offering roster endpoint");
assert.ok(/status:\s*"approved"/.test(pageSource), "page must explicitly request only approved enrollments");

console.log("PASS: Course Students is navigable by super_admin/hod/the 4 scoped Incharge Academic Cell roles/dpgs and calls the real backend endpoints");
