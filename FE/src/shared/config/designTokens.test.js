import { describe, it, expect } from "vitest";
import { DESIGN_TOKENS } from "./designTokens.js";

describe("Cold-Chain Control Tower Design Tokens", () => {
  it("defines the three mandatory SOP threshold gates", () => {
    const columns = DESIGN_TOKENS.thresholds.map((t) => t.column);
    expect(columns).toEqual(["IOT_TEMP_VAL_C", "PRT_CNG_LVL", "DELAY_PROB_DEC"]);
  });

  it("provides quick operator dispatch prompts", () => {
    expect(DESIGN_TOKENS.quickPrompts.length).toBeGreaterThanOrEqual(3);
  });
});
