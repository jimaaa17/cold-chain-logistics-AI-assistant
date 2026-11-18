#!/usr/bin/env node
/**
 * Pretty Mermaid CLI renderer (powered by beautiful-mermaid)
 * Supports rendering Mermaid (.mmd) into:
 *   - Beautiful themed SVGs (--format svg, default)
 *   - Terminal / Markdown ASCII or Unicode box-drawing art (--format ascii)
 *
 * Usage:
 *   node .agents/skills/pretty-mermaid/scripts/render.mjs \
 *     --input diagram.mmd \
 *     --output diagram.svg \
 *     --format svg \
 *     --theme tokyo-night
 */

import fs from 'node:fs';
import path from 'node:path';
import {
  THEMES,
  renderMermaidSVG,
  renderMermaidASCII,
} from '../node_modules/beautiful-mermaid/dist/index.js';

function parseArgs(argv) {
  const args = {
    input: null,
    output: null,
    format: 'svg', // 'svg' | 'ascii'
    theme: 'tokyo-night',
    useAscii: false,
    listThemes: false,
  };

  for (let i = 2; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === '--input' || arg === '-i') args.input = argv[++i];
    else if (arg === '--output' || arg === '-o') args.output = argv[++i];
    else if (arg === '--format' || arg === '-f') args.format = argv[++i];
    else if (arg === '--theme' || arg === '-t') args.theme = argv[++i];
    else if (arg === '--use-ascii') args.useAscii = true;
    else if (arg === '--list-themes') args.listThemes = true;
  }
  return args;
}

const args = parseArgs(process.argv);

if (args.listThemes) {
  console.log('Available themes:', Object.keys(THEMES).join(', '));
  process.exit(0);
}

if (!args.input) {
  console.error('Error: --input <file.mmd> is required.');
  process.exit(1);
}

const mmdSource = fs.readFileSync(args.input, 'utf8');

let result;
if (args.format === 'ascii' || args.format === 'unicode') {
  result = renderMermaidASCII(mmdSource, {
    useAscii: args.useAscii,
  });
} else {
  const themeObj = THEMES[args.theme] || THEMES['tokyo-night'];
  result = renderMermaidSVG(mmdSource, themeObj);
}

if (args.output) {
  fs.mkdirSync(path.dirname(path.resolve(args.output)), { recursive: true });
  fs.writeFileSync(args.output, result, 'utf8');
  console.log(`Rendered ${args.format.toUpperCase()} (${args.theme}) -> ${args.output}`);
} else {
  process.stdout.write(result + '\n');
}
