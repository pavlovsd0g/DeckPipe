import { build } from 'esbuild';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const frontend = path.join(root, 'frontend');

function readArg(name, fallback) {
  const index = process.argv.indexOf(name);
  if (index === -1) return fallback;
  const value = process.argv[index + 1];
  if (!value || value.startsWith('--')) {
    throw new Error(`${name} requires a path`);
  }
  return path.resolve(root, value);
}

const targets = [
  readArg('--app-static', path.join(root, 'app', 'static')),
  readArg('--desktop-ui', path.join(root, 'desktop', 'ui')),
];

const js = await build({
  entryPoints: [path.join(frontend, 'app.js')],
  outfile: 'app.js',
  bundle: true,
  format: 'esm',
  platform: 'browser',
  target: 'es2022',
  charset: 'utf8',
  sourcemap: false,
  legalComments: 'none',
  write: false,
  logLevel: 'silent',
});

const outputs = new Map(
  js.outputFiles.map(file => [path.basename(file.path), file.contents]),
);
outputs.set('index.html', await readFile(path.join(frontend, 'index.html')));
outputs.set('styles.css', await readFile(path.join(frontend, 'styles.css')));

for (const target of targets) {
  await mkdir(target, {recursive: true});
  for (const [name, bytes] of outputs) {
    await writeFile(path.join(target, name), bytes);
  }
}
