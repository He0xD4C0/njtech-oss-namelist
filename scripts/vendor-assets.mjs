import { mkdir, readFile, writeFile, copyFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const assets = [
  ['@tabler/core/dist/css/tabler.min.css', 'tabler/tabler.min.css'],
  ['@tabler/core/dist/js/tabler.min.js', 'tabler/tabler.min.js'],
  ['easymde/dist/easymde.min.css', 'easymde/easymde.min.css'],
  ['easymde/dist/easymde.min.js', 'easymde/easymde.min.js'],
  ['font-awesome/css/font-awesome.min.css', 'fontawesome/css/font-awesome.min.css'],
  ['font-awesome/fonts/fontawesome-webfont.woff2', 'fontawesome/fonts/fontawesome-webfont.woff2'],
  ['font-awesome/fonts/fontawesome-webfont.woff', 'fontawesome/fonts/fontawesome-webfont.woff'],
  ['font-awesome/fonts/fontawesome-webfont.ttf', 'fontawesome/fonts/fontawesome-webfont.ttf'],
  ['font-awesome/fonts/fontawesome-webfont.eot', 'fontawesome/fonts/fontawesome-webfont.eot'],
  ['font-awesome/fonts/fontawesome-webfont.svg', 'fontawesome/fonts/fontawesome-webfont.svg'],
  ['@tabler/core/README.md', 'tabler/README-and-license.md'],
  ['easymde/LICENSE', 'easymde/LICENSE'],
  ['font-awesome/README.md', 'fontawesome/LICENSE-and-attribution.md'],
];
for (const [source, destination] of assets) {
  const output = path.join(root, 'static/vendor', destination);
  await mkdir(path.dirname(output), { recursive: true });
  if (source.endsWith('.css')) {
    // Do not allow a vendor stylesheet to import a remote font at runtime.
    const css = (await readFile(path.join(root, 'node_modules', source), 'utf8'))
      .replace(/@import\s+(?:url\([^;]*https?:[^;]*\)|["'][^"']*https?:[^"']*["'])\s*;/g, '')
      .replace(/\/\*# sourceMappingURL=.*?\*\//g, '');
    await writeFile(output, css);
  } else {
    await copyFile(path.join(root, 'node_modules', source), output);
  }
}
