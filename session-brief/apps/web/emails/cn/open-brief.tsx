import type { BriefObject } from "@/lib/contracts/brief";
import { OpenBrief as UsOpenBrief } from "../open-brief";

// CN open email (D31): a thin, configured wrapper over the shared open
// template. Every CN specific — ¥, "vs CSI 300", the CN kind label — lives
// only here; the US template's own output is untouched by any of it.
export function OpenBrief({ brief }: { brief: BriefObject }) {
  return (
    <UsOpenBrief
      brief={brief}
      options={{
        currencySymbol: "¥",
        benchmarkLabel: "vs CSI 300",
        kindLabel: "CN Open",
      }}
    />
  );
}
