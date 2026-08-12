import type { BriefObject } from "@/lib/contracts/brief";
import { render } from "@react-email/render";
import { createElement } from "react";
import { CloseBrief } from "./close-brief";

// One source of truth for both parts: the HTML and the plaintext alternative are
// rendered from the same component, so they can never drift (docs/06).
export async function renderClose(brief: BriefObject): Promise<{ html: string; text: string }> {
  const el = createElement(CloseBrief, { brief });
  const [html, text] = await Promise.all([
    render(el, { pretty: false }),
    render(el, { plainText: true }),
  ]);
  return { html, text };
}
