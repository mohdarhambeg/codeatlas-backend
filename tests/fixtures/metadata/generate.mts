// Regeneration only: runs the existing TypeScript parser/builder on fixed fixtures.
// Usage: /path/to/codeatlas/node_modules/.bin/tsx generate.mts /path/to/codeatlas
import {pathToFileURL} from 'node:url';
import {readFileSync, writeFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {resolve, dirname} from 'node:path';
const backend = process.argv[2];
if (!backend) throw new Error('Pass the TypeScript backend path');
const {composeParsers} = await import(pathToFileURL(resolve(backend, 'src/modules/analysis/parser-composition.ts')).href);
const {AstBuilder} = await import(pathToFileURL(resolve(backend, 'src/modules/ast-management/builder.ts')).href);
const {serializeAst, verifyAstSource} = await import(pathToFileURL(resolve(backend, 'src/modules/ast-management/serializer.ts')).href);
for (const filename of ['coverage.jsx', 'errors.js', 'empty.js']) {
  const target = new URL(filename, import.meta.url);
  const sourceCode = readFileSync(target, 'utf8');
  const outcome = composeParsers().manager.parse({projectId: 'PRJ-123ABC', relativePath: filename, language: 'JavaScript', sourceCode});
  if (outcome.status === 'failed') throw new Error(outcome.message);
  const document = new AstBuilder().build(outcome, {analysisId: 'AN-11111111-1111-4111-8111-111111111111', language: 'JavaScript', contentHash: createHash('sha256').update(sourceCode).digest('hex'), createdAt: '2026-11-09T00:00:00.000Z'});
  verifyAstSource(document, sourceCode);
  writeFileSync(new URL(filename + '.ast.json', import.meta.url), serializeAst(document) + '\n');
}
