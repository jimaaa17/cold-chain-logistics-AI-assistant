---
name: figma-designer
description: >-
  Use this skill when designing control-tower screens in Figma, turning a Figma frame into FE/
  React code, syncing design tokens between Figma Variables and FE/, or auditing a Figma file
  or mockup against FE/DESIGN.md. Covers Figma MCP (read and write), token export, frame and
  component conventions, and the anti-slop review gate.
---

# Figma Designer Workflow

`FE/DESIGN.md` is the source of truth for every visual decision. Figma is where screens get
designed and reviewed, and it must never introduce a token, color, or pattern that DESIGN.md
doesn't define. When the two disagree, DESIGN.md wins: fix the Figma file, or change
DESIGN.md first with the user's agreement.

## Step 0: Detect the Figma connection

List the available tools and look for a Figma MCP server (tool names usually contain `figma`,
for example `get_design_context`, `get_variable_defs`, `get_screenshot`, `get_metadata`).
Names differ between server versions, so use what is actually listed.

- **Figma MCP connected:** use the read and write paths below.
- **Not connected:** say so in one line, then work offline. Produce the token file and a
  self-contained HTML mockup in `FE/design-mockups/`, which the user can import into Figma
  (for example with an HTML-to-Figma plugin or the MCP page-capture tool once it is connected).
  Never invent node IDs, file keys, or screenshots.

## Step 1: Tokens first

1. Load [figma-tokens.json](./references/figma-tokens.json). It is DESIGN.md expressed as
   W3C Design Tokens (DTCG), which Tokens Studio and most Figma Variables importers accept.
2. In Figma, tokens live in two Variable collections: `Color` (canvas, surface, border, ink,
   accent, signal) and `Type / Space` (font families, sizes, spacing, radii). Mode: `Light`
   only. The warm editorial palette has no dark mode.
3. If DESIGN.md changes, update `figma-tokens.json` in the same change, then re-import.
   When pulling from Figma (`get_variable_defs`), diff the result against `figma-tokens.json`
   and report any drift. Don't silently adopt Figma values.

## Step 2: Design in Figma (write path)

Follow [figma-conventions.md](./references/figma-conventions.md) for page layout, frame
sizes, the 12-column ledger grid, component naming, and auto-layout rules. Short version:

- Desktop frame `1440 × 900`, canvas fill `color/canvas`, 12-column grid, 24px gutter.
- Every fill, stroke, and text style is bound to a Variable or style. No raw hex values.
- Icons are 16px or 18px, 1.5px stroke, outline only, from one icon set. No emoji.
- Status is a 6px dot plus text. No colored pills, no tinted boxes inside cards.
- Use real domain content: shipment IDs, `6.1°C`, `$48,000`, SOP clause numbers, the SSE
  phases (`status → tool_input → tool_output → final_answer`). No lorem ipsum.
- Design every state the code has: idle, streaming, tool running, degraded (sentinel banners
  such as `SOP_KNOWLEDGE_BASE_UNAVAILABLE`), simulated (`AGENT_OFFLINE_DEMO` amber banner),
  error, and empty.

## Step 3: Figma to code (read path)

1. Get the frame's structure and screenshot through the MCP (`get_design_context` /
   `get_metadata` + `get_screenshot` on the selected node or the node ID from the URL).
2. Map Figma Variables to FE tokens. Don't paste generated Tailwind classes with raw hex.
3. Place code by feature slice (`FE/src/features/<domain>/`, shared primitives in
   `FE/src/shared/ui/`) per `FE/AGENTS.md` and the `fe-product-ai-architect` skill.
4. Treat generated code as a layout reference only. Rebuild it with the existing primitives,
   hooks, and SSE contract. Never wire mock data where the real stream belongs.
5. Run `cd FE && npm test && npm run build`.

## Step 4: Anti-slop review gate

Before handing over any frame, mockup, or component, check it against DESIGN.md section 1 and
report pass/fail for each item:

| Check | Fails if |
|---|---|
| Icons | Any emoji, filled icon, or stroke other than 1.5px |
| Nesting | A grey or tinted box inside a bordered card |
| Status color | More than the 3 signal colors, or any pill or badge fill |
| Effects | Gradient text, glow, or shadow stronger than `0 1px 2px rgba(0,0,0,0.03)` |
| Color | Any unbound or off-palette hex, `#000000`, or `#FFFFFF` used as the canvas |
| Accent | `#0F52BA` used on anything other than a primary CTA or focus state |
| Numbers | Money, temperatures, or times not in tabular or mono figures |
| Ledger grid | A decision row not on the 3 / 4 / 3 / 2 column split |

Fix the failures before presenting the result. Don't just list them.
