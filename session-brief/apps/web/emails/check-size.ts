// The machine-checkable half of the M9 done-when: render the frozen close fixture
// and fail if the HTML would clip in Gmail (budget 80KB) or the plaintext part is
// empty. Run with `pnpm --filter web email:check`.
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { BriefObject } from "@/lib/contracts/brief";
import { renderClose } from "./render";

const LIMIT = 80 * 1024;

async function main(): Promise<void> {
  const here = dirname(fileURLToPath(import.meta.url));
  const fixturePath = resolve(here, "../../worker/tests/fixtures/close_brief.json");
  const brief = JSON.parse(readFileSync(fixturePath, "utf8")) as BriefObject;

  const { html, text } = await renderClose(brief);
  const bytes = Buffer.byteLength(html, "utf8");

  console.log(
    `close email: ${bytes.toLocaleString()} bytes (${(bytes / 1024).toFixed(1)} KB), ` +
      `plaintext ${text.length} chars`,
  );

  let ok = true;
  if (bytes >= LIMIT) {
    console.error(`FAIL: html ${bytes} ≥ ${LIMIT} — Gmail clips near 102KB, budget is 80KB`);
    ok = false;
  }
  if (!text.trim()) {
    console.error("FAIL: empty plaintext part (a missing text/plain alternative is a spam signal)");
    ok = false;
  }
  if (!ok) process.exit(1);
  console.log("OK: under 80KB with a non-empty plaintext part");
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
