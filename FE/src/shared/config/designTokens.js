/**
 * Enterprise Cold-Chain Control Tower Design Tokens
 * Mapped from src/ui.py & Cold_Chain_Incident_SOP_v2.md thresholds.
 */
export const DESIGN_TOKENS = {
  colors: {
    canvas: "#0B0E14",
    sidebar: "#111622",
    card: "#111622",
    border: "#1E293B",
    textPrimary: "#E2E8F0",
    textMuted: "#94A3B8",
    accentCyan: "#38BDF8",
  },
  thresholds: [
    {
      id: "temp_excursion",
      label: "Thermal Breach Gate",
      column: "IOT_TEMP_VAL_C",
      rule: "> 4.0°C",
      severity: "critical",
    },
    {
      id: "port_congestion",
      label: "Port Congestion Gate",
      column: "PRT_CNG_LVL",
      rule: "> 7.0",
      severity: "warning",
    },
    {
      id: "delay_prob",
      label: "High Risk Delay Gate",
      column: "DELAY_PROB_DEC",
      rule: "> 0.65",
      severity: "critical",
    },
  ],
  quickPrompts: [
    {
      id: "excursion-scan",
      icon: "🌡️",
      label: "Scan Thermal Excursions > 4.0°C",
      prompt:
        "Query active fleet telemetry for any reefer units where Current_Temperature_C > 4.0 and retrieve the mandatory SOP mitigation steps.",
    },
    {
      id: "port-congestion",
      icon: "⚓",
      label: "Port Congestion > 7.0 & Weather",
      prompt:
        "Identify shipments with Port_Congestion_Level > 7.0, check live corridor weather at their GPS coordinates, and recommend dispatch actions.",
    },
    {
      id: "high-delay-risk",
      icon: "🚨",
      label: "High Delay Risk (> 0.65) Audit",
      prompt:
        "List High_Risk shipments with Delay_Probability > 0.65 and summarize the Cold-Chain SOP escalation protocol.",
    },
  ],
};
