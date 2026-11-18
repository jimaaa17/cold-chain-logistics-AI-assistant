# FE/DESIGN.md — Anti-Slop Design System & Constraints

This document is the binding design brain for `FE/`. Every mockup (`FE/design-mockups/*.html`) and React component (`FE/src/**`) must obey these rules so our product never looks like generic "vibe-coded" AI output.

## 1. Banned "AI Slop" Patterns (Zero Tolerance)
1. **No Emoji Icons Anywhere:** Never use `💰`, `🌡️`, `🏭`, `🧊`, `🔍`, `⚖️`, or `📞` in the UI. Use only crisp, 1.5px stroke inline SVG icons (`16px` or `18px`).
2. **No "Cards Inside Cards":** Never place grey or colored boxes (`bg-slate-100`, `bg-blue-50`) inside an already bordered card. Separate internal sections with `1px solid #EAEAEA` hairlines and whitespace (`24px`–`32px`).
3. **No Rainbow Pill Soup:** Never mix blue, red, amber, green, and purple pills on the same screen. Limit semantic status indicators to a `6px` status dot plus plain, high-contrast text.
4. **No Gradient Text, Neon Glows, or Heavy Shadows:** Keep shadows almost imperceptible (`0 1px 2px rgba(0,0,0,0.03)`). Rely on crisp `1px` borders (`#E6E6E2`) and typographic contrast.

## 2. Color Architecture (5-Step Warm Editorial Grayscale + 1 Brand Accent)
- **Canvas Background:** `#F7F7F5` (Warm architectural stone — never stark `#FFFFFF` or dark `#0B0E14` hacker black)
- **Elevated Surface:** `#FFFFFF` (Pure white reserved strictly for primary interactive surfaces)
- **Subtle Surface / Hover:** `#F0F0EC`
- **Hairline Border:** `#E6E6E2` (Outer containers) and `#EFECE6` (Internal row dividers)
- **Ink Hierarchy:**
  - Primary Ink: `#111110` (Deep carbon — never `#000000`)
  - Secondary Ink: `#575653` (Editorial slate)
  - Muted / Label Ink: `#8C8B85`
- **Single Brand Action Accent:** `#0F52BA` (Deep International Klein/Executive Blue — used only for primary decision CTAs and active focus states)
- **Restrained Semantic Signals (Dots + Text Only):**
  - Critical Risk: `#C81E1E`
  - Warning / Delay: `#B45309`
  - Healthy / On Schedule: `#15803D`

## 3. Typography & Tabular Precision
- **Primary Sans:** `'Inter'`, `-apple-system`, `BlinkMacSystemFont`, sans-serif with `letter-spacing: -0.015em` on headings.
- **Data & Financial Figures:** `'JetBrains Mono'` or `font-variant-numeric: tabular-nums` so dollar exposures (`$48,000`), temperatures (`6.1°C`), and times (`14:30`) align to the pixel.
- **Strict Left-Aligned Grid:** Every row in the decision ledger uses a deterministic 12-column CSS grid (`3 cols` Shipment & Exposure, `4 cols` Root Cause Diagnosis, `3 cols` Recommended Intervention, `2 cols` Decision Controls).
