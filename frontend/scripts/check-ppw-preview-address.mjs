#!/usr/bin/env node
// Regression check (no frontend test framework exists in this repo, so this is a
// plain Node script rather than a new test dependency): the PPW preview panel must
// never regress to the historical "AAU, Jorhat-785013" address, and must show
// AVFU's real address, matching backend/app/templates/ppw_document.html.
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const pagePath = path.join(__dirname, "..", "app", "(app)", "ppw", "page.tsx");
const source = fs.readFileSync(pagePath, "utf8");

assert.ok(
  source.includes("AVFU, Khanapara - 781022"),
  `PPW preview must contain AVFU's correct address "AVFU, Khanapara - 781022" (${pagePath})`,
);
assert.ok(
  !/AAU,\s*Jorhat/i.test(source),
  `PPW preview must never contain the historical "AAU, Jorhat" address (${pagePath})`,
);
assert.ok(
  !source.includes("785013") && !source.includes("785 013"),
  `PPW preview must never contain AAU's Jorhat PIN code (${pagePath})`,
);

console.log("PASS: PPW preview uses AVFU's correct institutional address, no AAU/Jorhat branding");
