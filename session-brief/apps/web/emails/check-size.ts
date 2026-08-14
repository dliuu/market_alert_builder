// The machine-checkable half of the M9 done-when, now covering both templates:
// render each frozen fixture and fail if the HTML would clip in Gmail (budget
// 80KB) or the plaintext part is empty. Run with `pnpm --filter web email:check`.
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { BriefObject } from "@/lib/contracts/brief";
import { renderBrief } from "./render";

const LIMIT = 80 * 1024;

// Both kinds, so the open brief can never quietly regress past the budget or
// lose its plaintext part (docs/06).
const FIXTURES = ["close_brief.json", "open_brief.json"];

async function main(): Promise<void> {
  const here = dirname(fileURLToPath(import.meta.url));
  let ok = true;

  for (const name of FIXTURES) {
    const path = resolve(here, "../../worker/tests/fixtures", name);
    const brief = JSON.parse(readFileSync(path, "utf8")) as BriefObject;

    const { html, text } = await renderBrief(brief);
    const bytes = Buffer.byteLength(html, "utf8");

    console.log(
      `${brief.kind} email: ${bytes.toLocaleString()} bytes (${(bytes / 1024).toFixed(1)} KB), ` +
        `plaintext ${text.length} chars`,
    );

    if (bytes >= LIMIT) {
      console.error(`FAIL ${name}: html ${bytes} ≥ ${LIMIT} — Gmail clips near 102KB`);
      ok = false;
    }
    if (!text.trim()) {
      console.error(`FAIL ${name}: empty plaintext part (a missing alternative is a spam signal)`);
      ok = false;
    }
  }

  if (!ok) process.exit(1);
  console.log("OK: every template under 80KB with a non-empty plaintext part");
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
