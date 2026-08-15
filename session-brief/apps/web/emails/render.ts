import type { BriefObject } from "@/lib/contracts/brief";
import { render } from "@react-email/render";
import { createElement } from "react";
import { CloseBrief } from "./close-brief";
import { CloseBrief as CnCloseBrief } from "./cn/close-brief";
import { OpenBrief as CnOpenBrief } from "./cn/open-brief";
import { OpenBrief } from "./open-brief";

// One source of truth for both parts: the HTML and the plaintext alternative are
// rendered from the same component, so they can never drift (docs/06).
export async function renderClose(brief: BriefObject): Promise<{ html: string; text: string }> {
  return renderBoth(createElement(CloseBrief, { brief }));
}

export async function renderOpen(brief: BriefObject): Promise<{ html: string; text: string }> {
  return renderBoth(createElement(OpenBrief, { brief }));
}

export async function renderCnClose(brief: BriefObject): Promise<{ html: string; text: string }> {
  return renderBoth(createElement(CnCloseBrief, { brief }));
}

export async function renderCnOpen(brief: BriefObject): Promise<{ html: string; text: string }> {
  return renderBoth(createElement(CnOpenBrief, { brief }));
}

// "Different jobs, different templates" (docs/01) — the one place `kind` picks
// one, so the worker still makes a single render call (D6). Explicit four-way
// dispatch: an unknown kind throws rather than silently falling back to the
// close template (D31).
export async function renderBrief(brief: BriefObject): Promise<{ html: string; text: string }> {
  switch (brief.kind) {
    case "open":
      return renderOpen(brief);
    case "close":
      return renderClose(brief);
    case "open_cn":
      return renderCnOpen(brief);
    case "close_cn":
      return renderCnClose(brief);
    default:
      throw new Error(`renderBrief: unknown brief kind ${brief.kind as string}`);
  }
}

async function renderBoth(el: React.ReactElement): Promise<{ html: string; text: string }> {
  const [html, text] = await Promise.all([
    render(el, { pretty: false }),
    render(el, { plainText: true }),
  ]);
  return { html, text };
}
