# Figma File Conventions: Cold-Chain Control Tower

## Pages
1. `Cover`: product name, status (Draft / In review / Approved), last-synced date of the tokens.
2. `Tokens`: swatches and type specimens bound to Variables (a visual check of `figma-tokens.json`).
3. `Components`: the primitives library.
4. `Screens`: one section per feature slice (`Dispatch`, `Audit`, `Telemetry`).
5. `States`: the streaming, degraded, simulated, error, and empty variants of each screen.
6. `Archive`: superseded explorations. Never ship from here.

## Frames and grid
- Desktop `1440 × 900` (primary), Laptop `1280 × 800`, Mobile `390 × 844` (audit read-only).
- Layout grid: 12 columns, 24px gutter, 32px margin (desktop). 4 columns and 16px margin on mobile.
- Decision-ledger rows span: columns 1–3 for Shipment & Exposure, 4–7 for Root Cause Diagnosis,
  8–10 for Recommended Intervention, 11–12 for Decision Controls.
- Spacing scale (auto-layout gaps and padding only): 4, 8, 12, 16, 24, 32, 48.
- Section separation inside a card: a 1px `color/border/divider` line plus 24–32px space.

## Component naming
`Category/Name/Variant`, with properties in Figma variant syntax, matching the React names so the
read path maps 1:1:

| Figma component | React target |
|---|---|
| `Shell/Sidebar` | `FE/src/app/Sidebar.jsx` |
| `Dispatch/MessageBubble` (role=user, agent; simulated=true, false) | `features/dispatch/MessageBubble` |
| `Dispatch/ReasoningTimeline` (phase=status, tool_input, tool_output, final_answer, error) | `features/dispatch/ReasoningTimeline` |
| `Dispatch/ToolTraceAccordion` (open=true, false) | `features/dispatch/ToolTraceAccordion` |
| `Telemetry/KpiCard` (signal=healthy, warning, critical) | `features/telemetry/TelemetryKpiBar` |
| `Audit/LogRow`, `Audit/AdminGate` | `features/audit/AuditLogView` |
| `Primitive/Button` (kind=primary, secondary, ghost) | `shared/ui/Primitives.jsx` |
| `Primitive/StatusDot` (signal=healthy, warning, critical) | `shared/ui/Primitives.jsx` |
| `Primitive/Icon/<name>` (size=16, 18) | inline SVG |

## Auto-layout rules
- Every component uses auto-layout. No absolutely positioned children except overlays.
- Text layers: fixed width with hug height. Numeric columns are right-aligned and use the `type/mono` style.
- Corner radius: 6px for controls, 8px for cards. Nothing more rounded.
- Borders: 1px inside stroke. Cards use `color/border/outer`, rows use `color/border/divider`.

## Content rules
- Use realistic data in the legacy and alias vocabulary: `Current_Temperature_C 6.1°C`,
  `Port_Congestion_Level 7.8`, `Delay_Probability 0.72`, SOP thresholds `> 4.0°C`, `> 7.0`, `> 0.65`.
- Agent answers show the real Markdown template (a single shipment table plus an SOP citation).
  A design change to that template is also a change to `BE/agent/prompts/system_prompt.txt`, so flag it.

## Handoff checklist
- [ ] All fills, strokes, and text bound to Variables or styles (check with the Selection colors panel).
- [ ] Every state on the `States` page exists for the changed screen.
- [ ] The anti-slop review gate in SKILL.md passes.
- [ ] Frame links shared as `figma.com/design/<fileKey>/...?node-id=<id>` for the read path.
