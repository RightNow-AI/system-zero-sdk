import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import ts from 'typescript';
import { describe, expect, it } from 'vitest';

const fixtures: { code: number; goodbye: { reason: string | null } }[] = JSON.parse(readFileSync(new URL('../../fixtures/stream/close-codes.json', import.meta.url), 'utf8'));
const typeFile = fileURLToPath(new URL('../src/stream-interface.ts', import.meta.url));
// This literal-only field needs no library or imported HTTP type to check assignability.
const program = ts.createProgram([typeFile], { strictNullChecks: true, noLib: true, noResolve: true, types: [] });
const checker = program.getTypeChecker();
const source = program.getSourceFile(typeFile)!;
const exported = checker.getExportsOfModule(checker.getSymbolAtLocation(source)!);
const goodbye = checker.getDeclaredTypeOfSymbol(exported.find(symbol => symbol.name === 'StreamGoodbye')!);
const reason = checker.getPropertyOfType(goodbye, 'reason')!;
const reasonType = checker.getTypeOfSymbolAtLocation(reason, source);

describe('shared gateway close fixtures', () => {
  it.each(fixtures)('represents the gateway goodbye reason for $code/$goodbye.reason in the public type', ({ goodbye }) => {
    const literal = goodbye.reason === null ? checker.getNullType() : checker.getStringLiteralType(goodbye.reason);
    expect(checker.isTypeAssignableTo(literal, reasonType)).toBe(true);
  });
});
