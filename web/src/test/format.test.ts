import {describe, expect, it} from 'vitest';
import {safeMarkdownUrl, safeUrl} from '../lib/format';

describe('Markdown download URLs',()=>{
 const file='/v1/jobs/job-123/artifacts/file_456/file';
 it('allows only the exact internal artifact download route without adding an origin',()=>{
  expect(safeMarkdownUrl(file)).toBe(file);
  expect(safeUrl(file)).toBeUndefined();
  expect(safeMarkdownUrl('https://example.org/article.pdf')).toBe('https://example.org/article.pdf');
 });
 it.each([
  '//evil.example/v1/jobs/job-123/artifacts/file_456/file',
  '/v1/jobs/job-123/resume',
  '/v1/connections/provider/approve',
  '/v1/jobs/../artifacts/file_456/file',
  '/v1/jobs/%2e%2e/artifacts/file_456/file',
  '/v1/jobs/job-123/artifacts/file_456/file/../../resume',
  '/v1/jobs/job-123/artifacts/file_456/file?redirect=https://evil.example',
  '/v1/jobs/job-123/artifacts/file_456/file#fragment',
  '/v1/jobs/job-123/artifacts/file_456/file/',
  'javascript:alert(1)',
  'data:text/html,unsafe',
  'file:///etc/passwd',
  undefined,
 ])('rejects non-download or ambiguous internal URLs: %s',value=>{
  expect(safeMarkdownUrl(value)).toBeUndefined();
 });
});
