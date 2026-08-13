import type { BriefObject } from "@/lib/contracts/brief";
import { render } from "@react-email/render";
import { createElement } from "react";
import { CloseBrief } from "./close-brief";
import { OpenBrief } from "./open-brief";

// One source of truth for both parts: the HTML and the plaintext alternative are
// rendered from the same component, so they can never drift (docs/06).
export async function renderClose(brief: BriefObject): Promise<{ html: string; text: string }> {
  return renderBoth(createElement(CloseBrief, { brief }));
}

export async function renderOpen(brief: BriefObject): Promise<{ html: string; text: string }> {
  return renderBoth(createElement(OpenBrief, { brief }));
}

// "Different jobs, different templates" (docs/01) — the one place `kind` picks
// one, so the worker still makes a single render call (D6).
export async function renderBrief(brief: BriefObject): Promise<{ html: string; text: string }> {
  return brief.kind === "open" ? renderOpen(brief) : renderClose(brief);
}

async function renderBoth(el: React.ReactElement): Promise<{ html: string; text: string }> {
  const [html, text] = await Promise.all([
    render(el, { pretty: false }),
    render(el, { plainText: true }),
  ]);
  return { html, text };
}
