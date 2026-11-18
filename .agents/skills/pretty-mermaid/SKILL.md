---
name: pretty-mermaid
description: >-
  Render Mermaid diagrams as beautiful SVGs or ASCII/Unicode art using beautiful-mermaid.
  Use when creating, styling, or replacing diagrams with themed SVGs (15+ built-in themes like
  tokyo-night, dracula, github-dark, catppuccin-mocha, nord) or terminal-friendly ASCII box drawings.
---

# Pretty Mermaid Skill (SVG & ASCII Art Renderer)

Render `.mmd` Mermaid definitions into zero-DOM **beautiful SVGs** or **terminal/markdown ASCII art** via [scripts/render.mjs](./scripts/render.mjs) (powered by `beautiful-mermaid`).

## Commands

1. **Render a Beautiful Themed SVG**:
   ```bash
   node .agents/skills/pretty-mermaid/scripts/render.mjs \
     --input <path/to/diagram.mmd> \
     --output <path/to/diagram.svg> \
     --format svg \
     --theme tokyo-night
   ```

2. **Render Terminal / Markdown ASCII & Unicode Box-Drawing Art**:
   ```bash
   node .agents/skills/pretty-mermaid/scripts/render.mjs \
     --input <path/to/diagram.mmd> \
     --format ascii
   ```
   *(Add `--use-ascii` for pure 7-bit ASCII characters instead of Unicode box-drawing characters, or `--output <file.txt>` to save to disk.)*

3. **List Built-in Themes (15 themes)**:
   ```bash
   node .agents/skills/pretty-mermaid/scripts/render.mjs --list-themes
   ```
   Available themes: `tokyo-night`, `tokyo-night-storm`, `tokyo-night-light`, `catppuccin-mocha`, `catppuccin-latte`, `nord`, `nord-light`, `dracula`, `github-light`, `github-dark`, `solarized-light`, `solarized-dark`, `one-dark`, `zinc-light`, `zinc-dark`.

## References
- See [theme-templates.md](./references/theme-templates.md) for theme palettes and diagram templates.
