import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';

const dependencies = createRequire(new URL('./typescript/package.json', import.meta.url));

export default {
  root: fileURLToPath(new URL('.', import.meta.url)),
  cacheDir: './tmp/vite',
  resolve: {
    alias: {
      vitest: fileURLToPath(new URL('./dist/index.js', pathToFileURL(dependencies.resolve('vitest/package.json')))),
      ...Object.fromEntries(['typescript'].map(name => [name, dependencies.resolve(name)])),
    },
  },
  test: {
    include: ['typescript/tests/**/*.test.ts', 'integrations/**/*.test.ts'],
    environment: 'node',
    maxWorkers: 2,
    fileParallelism: false,
    testTimeout: 15000,
    hookTimeout: 15000,
    restoreMocks: true,
    unstubEnvs: true,
  },
};
