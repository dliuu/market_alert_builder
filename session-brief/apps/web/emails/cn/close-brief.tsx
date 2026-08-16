import type { BriefObject } from "@/lib/contracts/brief";
import { CloseBrief as UsCloseBrief } from "../close-brief";

// CN close email (D32): a thin, configured wrapper over the shared close
// template. Every CN specific — ¥, "vs CSI 300", the CN kind label — lives
// only here; the US template's own output is untouched by any of it.
//
// CN-M4: the "Yesterday's flag, resolved" block (`brief.resolved_claims`) is
// part of that shared template and needs no CN-specific wiring here — it
// renders for a CN close brief exactly as it does for a US one the moment
// `resolve_due_claims(market="CN", ...)` starts populating the field.
export function CloseBrief({ brief }: { brief: BriefObject }) {
  return (
    <UsCloseBrief
      brief={brief}
      options={{
        currencySymbol: "¥",
        benchmarkLabel: "vs CSI 300",
        kindLabel: "CN Close",
        closeLabel: "session closed 15:00 CST",
      }}
    />
  );
}
