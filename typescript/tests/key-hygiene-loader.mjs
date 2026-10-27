import { registerHooks, createRequire } from 'node:module';
import { readFileSync, existsSync } from 'node:fs';
const require = createRequire(import.meta.url);
const ts = require('typescript');
registerHooks({
  resolve(specifier, context, next) {
    if (specifier.endsWith('.js') && (specifier.startsWith('.') || specifier.startsWith('file:'))) {
      const url = new URL(specifier, context.parentURL).href.replace(/\.js$/, '.ts');
      if (existsSync(new URL(url))) return { url, shortCircuit: true };
    }
    return next(specifier, context);
  },
  load(url, context, next) {
    if (url.endsWith('.ts')) return { format: 'module', shortCircuit: true,
      source: ts.transpileModule(readFileSync(new URL(url), 'utf8'), {
        compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
      }).outputText };
    return next(url, context);
  },
});
