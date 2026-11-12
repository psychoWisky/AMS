#!/usr/bin/env node
// Regression check (no frontend test framework in this repo — plain Node script,
// same convention as check-course-students.mjs): the Course Registration Card
// preview must group courses by each enrollment's own `classification`
// (Major / Minor / Supporting / Compulsory Credit), never by `Course.category`,
// matching the backend PDF (`_build_registration_card_context`).
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const file = path.join(__dirname, "..", "app", "(app)", "course-registration", "page.tsx");
const src = fs.readFileSync(file, "utf8");

// 1. The four approved classification headings, in the approved order.
const m = src.match(/const CARD_CLASSIFICATIONS[^=]*=\s*\[([\s\S]*?)\];/);
assert.ok(m, "CARD_CLASSIFICATIONS constant must exist");
const entries = [...m[1].matchAll(/key:\s*"([^"]+)"\s*,\s*label:\s*"([^"]+)"/g)].map((x) => [x[1], x[2]]);
assert.deepEqual(entries, [
  ["major", "Major Courses"],
  ["minor", "Minor Courses"],
  ["supporting", "Supporting Courses"],
  ["compulsory", "Compulsory Credit Courses"],
], "CARD_CLASSIFICATIONS must be exactly Major, Minor, Supporting, Compulsory Credit — in that order");

// 2. previewGroups is derived from the classification, not the catalogue category.
const pg = src.match(/const previewGroups\s*=([\s\S]*?);\s*\n/);
assert.ok(pg, "previewGroups must be defined");
assert.ok(pg[1].includes("CARD_CLASSIFICATIONS"), "previewGroups must iterate CARD_CLASSIFICATIONS");
assert.ok(/\.classification\s*===\s*key/.test(pg[1]), "previewGroups must filter rows by `classification`");
assert.ok(!/\.category\b/.test(pg[1]), "previewGroups must not reference `category`");

// 3. The old category-based grouping/labels are gone.
assert.ok(!/\bCATEGORY_LABELS\b/.test(src), "the old CATEGORY_LABELS mapping must not be used");
assert.ok(!/\bit\.category\b/.test(src), "no `it.category` grouping may remain on the card page");

// 4. No fallback heading; invalid classification is reported, not hidden.
assert.ok(!/["`](Other|Unclassified)[^"`]*["`]/i.test(src), "no Other/Unclassified heading label");
assert.ok(/invalidClassificationItems/.test(src), "invalid/NULL classification must be surfaced as a data-integrity warning");

console.log("OK: registration card preview groups by classification (4 headings, ordered, no category fallback)");
