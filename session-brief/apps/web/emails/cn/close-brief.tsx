import type { BriefObject } from "@/lib/contracts/brief";
import { CloseBrief as UsCloseBrief } from "../close-brief";

// CN close email (D32): a thin, configured wrapper over the shared close
// template. Every CN specific — ¥, "vs CSI 300", the CN kind label — lives
// only here; the US template's own output is untouched by any of it.
export function CloseBrief({ brief }: { brief: BriefObject }) {
  return (
    <UsCloseBrief
      brief={brief}
      options={{
        currencySymbol: "¥",
        benchmarkLabel: "vs CSI 300",
        kindLabel: "CN Close",
      }}
    />
  );
}
