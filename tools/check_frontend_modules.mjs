#!/usr/bin/env node
// Parse and link the browser ES-module graph without executing DOM-dependent code.
import fs from 'node:fs/promises';
import path from 'node:path';
import vm from 'node:vm';
import {fileURLToPath, pathToFileURL} from 'node:url';

if (!vm.SourceTextModule) {
  throw new Error('Run with: node --experimental-vm-modules tools/check_frontend_modules.mjs');
}

const root = path.resolve(process.argv[2] || 'web/static/app.js');
const modules = new Map();

async function loadModule(filename) {
  const absolute = path.resolve(filename);
  if (modules.has(absolute)) return modules.get(absolute);
  const source = await fs.readFile(absolute, 'utf8');
  const module = new vm.SourceTextModule(source, {
    identifier: pathToFileURL(absolute).href,
  });
  modules.set(absolute, module);
  await module.link(async (specifier, referencingModule) => {
    const parent = path.dirname(fileURLToPath(referencingModule.identifier));
    return loadModule(path.resolve(parent, specifier));
  });
  return module;
}

await loadModule(root);
console.log(`Frontend module graph linked: ${modules.size} modules`);
