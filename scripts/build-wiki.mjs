import { existsSync } from 'node:fs';
import { readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const rootDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const sourcePath = path.join(rootDir, 'content', 'wiki.playervfdoc');
const outputPath = path.join(rootDir, 'wiki.html');

const source = await readWithIncludes(sourcePath);
const document = parsePlayerVFDoc(source);
const html = renderPage(document);
await writeFile(outputPath, html, 'utf8');

console.log(`Generated ${path.relative(rootDir, outputPath)} from ${document.sections.length} sections.`);

async function readWithIncludes(filePath, seen = new Set()) {
  const resolvedPath = path.resolve(filePath);
  if (seen.has(resolvedPath)) {
    throw new Error(`Circular include detected for ${resolvedPath}`);
  }
  seen.add(resolvedPath);

  const text = await readFile(resolvedPath, 'utf8');
  const baseDir = path.dirname(resolvedPath);
  const lines = [];

  for (const line of text.split(/\r?\n/)) {
    const includeMatch = line.match(/^@include\s+(.+)$/);
    if (!includeMatch) {
      lines.push(line);
      continue;
    }

    const includePath = path.resolve(baseDir, includeMatch[1].trim());
    lines.push(await readWithIncludes(includePath, seen));
  }

  seen.delete(resolvedPath);
  return lines.join('\n');
}

function parsePlayerVFDoc(text) {
  const metadata = {
    title: 'PlayerVF Full Wiki',
    description: 'PlayerVF documentation.',
    heroLabel: 'Wiki',
    heroTitle: 'PlayerVF documentation.',
    heroLead: '',
    stats: [],
    sources: []
  };
  const sections = [];
  let current = null;
  let mode = 'body';

  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trimEnd();
    const trimmed = line.trim();

    if (!trimmed) {
      continue;
    }

    if (trimmed.startsWith('@')) {
      applyMetadata(metadata, trimmed);
      continue;
    }

    const sectionMatch = trimmed.match(/^::section\s+([a-z0-9-]+)\s+"([^"]+)"$/i);
    if (sectionMatch) {
      current = {
        id: sectionMatch[1],
        title: sectionMatch[2],
        blocks: []
      };
      sections.push(current);
      mode = 'body';
      continue;
    }

    if (!current) {
      continue;
    }

    if (trimmed === '::files') {
      mode = 'files';
      current.blocks.push({ type: 'heading', level: 3, text: 'Directory map' });
      continue;
    }

    const photoMatch = trimmed.match(/^::photo\s+(\S+)\s+"([^"]*)"(?:\s+"([^"]*)")?$/);
    if (photoMatch) {
      current.blocks.push({
        type: 'photo',
        src: photoMatch[1],
        alt: photoMatch[2],
        caption: photoMatch[3] || ''
      });
      continue;
    }

    const pair = parsePair(trimmed);
    if (pair) {
      pushPair(current, pair, mode === 'files' ? 'files' : 'grid');
      continue;
    }

    pushText(current, trimmed);
    mode = 'body';
  }

  return { metadata, sections };
}

function applyMetadata(metadata, line) {
  const [key, ...restParts] = line.slice(1).split(/\s+/);
  const rest = restParts.join(' ').trim();

  if (key === 'title') {
    metadata.title = rest;
  } else if (key === 'description') {
    metadata.description = rest;
  } else if (key === 'hero-label') {
    metadata.heroLabel = rest;
  } else if (key === 'hero-title') {
    metadata.heroTitle = rest;
  } else if (key === 'hero-lead') {
    metadata.heroLead = rest;
  } else if (key === 'source') {
    metadata.sources.push(rest);
  } else if (key === 'stat') {
    const [value, label] = rest.split('|').map((part) => part.trim());
    if (value && label) {
      metadata.stats.push({ value, label });
    }
  }
}

function parsePair(line) {
  const index = line.indexOf('=>');
  if (index === -1) {
    return null;
  }

  return {
    title: line.slice(0, index).trim(),
    text: line.slice(index + 2).trim()
  };
}

function pushPair(section, pair, type) {
  const last = section.blocks.at(-1);
  if (last?.type === type) {
    last.items.push(pair);
    return;
  }

  section.blocks.push({ type, items: [pair] });
}

function pushText(section, text) {
  if (section.id === 'function-index' && !text.endsWith('.')) {
    const last = section.blocks.at(-1);
    if (last?.type === 'function-list') {
      last.items.push(text);
    } else {
      section.blocks.push({ type: 'function-list', items: [text] });
    }
    return;
  }

  const looksLikeIndex = /^[A-Za-z_][A-Za-z0-9_]*(?:[._][A-Za-z0-9_]+)+$/.test(text);
  const last = section.blocks.at(-1);

  if (looksLikeIndex) {
    if (last?.type === 'function-list') {
      last.items.push(text);
    } else {
      section.blocks.push({ type: 'function-list', items: [text] });
    }
    return;
  }

  if (last?.type === 'list') {
    last.items.push(text);
  } else if (text.length < 140 && !text.endsWith('.')) {
    section.blocks.push({ type: 'heading', level: 3, text });
  } else {
    section.blocks.push({ type: 'paragraph', text });
  }
}

function renderPage(document) {
  const { metadata, sections } = document;
  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>${escapeHtml(metadata.title)}</title>
  <meta name="description" content="${escapeAttribute(metadata.description)}">
  <meta name="theme-color" content="#07080d">
  <link rel="stylesheet" href="assets/css/base.css">
  <link rel="stylesheet" href="assets/css/wiki.css">
</head>
<body>
  <header class="site-header">
    <div class="wide-wrap nav">
      <a class="brand" href="index.html">PlayerVF</a>
      <nav class="links" aria-label="Main navigation">
        <a href="index.html">Downloads</a>
        <a href="wiki.html">Wiki</a>
      </nav>
    </div>
  </header>

  <main class="wide-wrap">
    <section class="hero">
      <span class="label">${escapeHtml(metadata.heroLabel)}</span>
      <h1>${escapeHtml(metadata.heroTitle)}</h1>
      <p class="lead">${formatInline(metadata.heroLead)}</p>
      ${renderStats(metadata.stats)}
    </section>

    <div class="layout">
      <aside class="toc" aria-label="Wiki sections">
        <strong>Documentation</strong>
        ${sections.map((section) => `<a href="#${escapeAttribute(section.id)}">${escapeHtml(section.title)}</a>`).join('\n        ')}
      </aside>

      <div class="content">
        ${sections.map(renderSection).join('\n\n        ')}
      </div>
    </div>
  </main>

  <footer>
    <div class="wide-wrap footer-row">
      <span>${escapeHtml(metadata.title)}</span>
      <a href="index.html">Back to downloads</a>
    </div>
  </footer>
</body>
</html>
`;
}

function renderStats(stats) {
  if (!stats.length) {
    return '';
  }

  return `<div class="doc-stats" aria-label="Documentation summary">
        ${stats.map((stat) => `<div class="doc-stat"><strong>${escapeHtml(stat.value)}</strong><span>${escapeHtml(stat.label)}</span></div>`).join('\n        ')}
      </div>`;
}

function renderSection(section) {
  return `<section id="${escapeAttribute(section.id)}" class="card">
          <h2>${escapeHtml(section.title)}</h2>
          ${section.blocks.map(renderBlock).join('\n          ')}
        </section>`;
}

function renderBlock(block) {
  if (block.type === 'paragraph') {
    return `<p>${formatInline(block.text)}</p>`;
  }

  if (block.type === 'heading') {
    return `<h${block.level}>${formatInline(block.text)}</h${block.level}>`;
  }

  if (block.type === 'grid') {
    return `<div class="grid">
            ${block.items.map((item) => `<div class="mini"><strong>${formatInline(item.title)}</strong><span>${formatInline(item.text)}</span></div>`).join('\n            ')}
          </div>`;
  }

  if (block.type === 'files') {
    return `<ul>
            ${block.items.map((item) => `<li><code>${escapeHtml(item.title)}</code>: ${formatInline(item.text)}</li>`).join('\n            ')}
          </ul>`;
  }

  if (block.type === 'function-list') {
    return `<ul class="function-list">
            ${block.items.map((item) => `<li><code>${escapeHtml(item)}</code></li>`).join('\n            ')}
          </ul>`;
  }

  if (block.type === 'photo') {
    if (!isRenderableImage(block.src)) {
      return `<figure class="wiki-photo missing-photo">
            <div class="photo-placeholder">Add image: <code>${escapeHtml(block.src)}</code></div>
            ${block.caption ? `<figcaption>${formatInline(block.caption)}</figcaption>` : ''}
          </figure>`;
    }

    const caption = block.caption ? `<figcaption>${formatInline(block.caption)}</figcaption>` : '';
    return `<figure class="wiki-photo">
            <img src="${escapeAttribute(block.src)}" alt="${escapeAttribute(block.alt)}" loading="lazy">
            ${caption}
          </figure>`;
  }

  if (block.type === 'list') {
    return `<ul>
            ${block.items.map((item) => `<li>${formatInline(item)}</li>`).join('\n            ')}
          </ul>`;
  }

  return '';
}

function isRenderableImage(src) {
  if (/^(https?:)?\/\//.test(src) || src.startsWith('data:')) {
    return true;
  }

  return existsSync(path.join(rootDir, src));
}

function formatInline(text) {
  return escapeHtml(text).replace(/`([^`]+)`/g, '<code>$1</code>');
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function escapeAttribute(value) {
  return escapeHtml(value).replace(/`/g, '&#96;');
}
