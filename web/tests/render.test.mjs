import test from 'node:test';
import assert from 'node:assert/strict';
import { readdir, readFile } from 'node:fs/promises';
import { validateGithubUrl, allowedActions, parseRoute, formatRequestModel, statusLabel, el } from '../public/render.js';

const base = 'https://github.com/example-org/Example-App';
const id = 'abcdefghijklmnopqrstuv';

test('GitHub links accept only canonical URLs, optionally bound to kind and number', () => {
  assert.equal(validateGithubUrl(`${base}/pull/1`), true);
  assert.equal(validateGithubUrl(`${base}/pull/9999999`, 'pull', 9999999), true);
  assert.equal(validateGithubUrl(`${base}/issues/42`, 'issues', 42), true);
});

const invalidUrls = [
  base.replace('https:', 'http:') + '/pull/1',
  'https://github.com/other/Example-App/pull/1',
  'https://github.com/example-org/other/pull/1',
  'https://github.com/example-org/example-app/pull/1',
  'https://GITHUB.com/example-org/Example-App/pull/1',
  `${base}/pull/1/files`, `${base}/pull/1?x=1`, `${base}/pull/1#x`, `${base}/pull/1/`,
  'javascript:alert(1)',
  'https://github.com@evil.com/example-org/Example-App/pull/1',
  'https://user@github.com/example-org/Example-App/pull/1',
  'https://user:pass@github.com/example-org/Example-App/pull/1',
  'https://github.com:443/example-org/Example-App/pull/1',
  'https://github.com:8443/example-org/Example-App/pull/1',
  `${base}/pull/0`, `${base}/pull/01`, `${base}/pull/12345678`, `${base}/pull/-1`,
  ` ${base}/pull/1`, `${base}/pull/1 `, `${base}/pull/1\n`, `${base}/pull/\n1`,
  `${base}/pull/%31`, `${base}/issues/1?`, `${base}/issues/1#`,
  null, 1, {}, undefined,
];
for (const url of invalidUrls) {
  test(`GitHub URL rejected: ${JSON.stringify(url)}`, () => assert.equal(validateGithubUrl(url), false));
}
test('GitHub links must match the displayed reference', () => {
  assert.equal(validateGithubUrl(`${base}/pull/1`, 'issues', 1), false);
  assert.equal(validateGithubUrl(`${base}/issues/1`, 'pull', 1), false);
  assert.equal(validateGithubUrl(`${base}/pull/1`, 'pull', 2), false);
  assert.equal(validateGithubUrl(`${base}/pull/1`, undefined, 2), false);
  assert.equal(validateGithubUrl(`${base}/pull/1`, 'pull', '1'), false);
});

test('every status allows exactly the specified actions, including expiry equality', () => {
  const expires = new Date('2026-10-06T12:00:00Z');
  const expected = {
    pending: ['yes', 'no'], approved: ['no'], declined: ['yes'],
    cancel_requested: [], expired: [], finished: [], unknown: [],
  };
  for (const [status, actions] of Object.entries(expected)) {
    assert.deepEqual(allowedActions(status, expires, new Date(expires.getTime() - 1)), actions, status);
    for (const time of [expires.getTime(), expires.getTime() + 1, expires.getTime() + 86_400_000]) {
      assert.deepEqual(allowedActions(status, expires, time), status === 'approved' ? ['no'] : [], `${status} at ${time}`);
    }
  }
  assert.deepEqual(allowedActions('pending', { toMillis: () => expires.getTime() }, expires.getTime() - 1), ['yes', 'no']);
  assert.deepEqual(allowedActions('pending', null, Date.now()), []);
  assert.deepEqual(allowedActions('declined', new Date('invalid'), Date.now()), []);
});

test('routes accept exactly the declared paths', () => {
  assert.deepEqual(parseRoute(`/r/${id}`), { name: 'request', id });
  assert.deepEqual(parseRoute('/r/ABC_def-GHI_jkl-MNO_12'), { name: 'request', id: 'ABC_def-GHI_jkl-MNO_12' });
  assert.deepEqual(parseRoute('/log'), { name: 'log' });
  assert.deepEqual(parseRoute('/'), { name: 'home' });
  for (const path of ['/log/', `/r/${id}/`, '/r/short', `/r/${id}x`, `/r/${id.slice(1)}`, '/r/abcdefghijklmnopqrstu!',
    `/r/${id}\n`, '/r/%2Fabcdefghijklmnopqrs', '/unknown', '/r/', `/r/${id}/events`, '', null]) {
    assert.deepEqual(parseRoute(path), { name: 'notfound' }, String(path));
  }
});

test('view models retain data as plain text, normalize timestamps, and mark both test modes', () => {
  const at = new Date('2026-10-06T00:00:00Z');
  const hostileTitle = '<img src=x onerror=alert(1)>';
  const request = {
    kind: 'tester-upload', test: true, buildNumberExpected: 124, lastUploadedBuild: 120, localBuildMax: 123,
    sha: 'a'.repeat(40), appCommits: 4, status: 'pending', version: 0,
    createdAt: { toMillis: () => at.getTime() }, expiresAt: new Date(at.getTime() + 43_200_000),
    decidedAt: null, decidedReason: null,
    prs: [{ number: 12, title: hostileTitle, url: `${base}/pull/12`, issues: [{ number: 7, title: null, url: `${base}/issues/7` }] }],
    history: [{ version: 0, status: 'pending', at, reason: 'created', via: 'system' }],
    pendingWebDecision: { decision: 'yes', expectedVersion: 0, eventId: 'event', at },
  };
  const model = formatRequestModel(request);
  assert.equal(model.title, '[TEST] Tester upload +124');
  assert.equal(model.prs[0].title, hostileTitle);
  assert.equal(model.prs[0].issues[0].title, '');
  assert.equal(model.createdAt, at.getTime());
  assert.equal(model.decidedAt, null);
  assert.equal(model.hasLocalBuilds, true);
  assert.equal(model.shortSha, 'aaaaaaaa');
  assert.equal(model.history[0].via, 'system');
  assert.equal(model.pendingWebDecision.eventId, 'event');
  assert.equal(formatRequestModel({ ...request, test: false, kind: 'test-echo' }).test, true);
  assert.equal(formatRequestModel({ ...request, test: false }).title, 'Tester upload +124');
  assert.equal(formatRequestModel(null).title, 'Tester upload +?');
  assert.equal(statusLabel('cancel_requested'), 'Cancel requested');
  assert.equal(statusLabel('__proto__'), 'Unknown');
});

test('DOM helper writes text and rejects attributes outside its fixed whitelist', () => {
  const fakeDocument = { createElement: (tag) => ({ tag, textContent: '', attrs: {}, setAttribute(name, value) { this.attrs[name] = value; } }) };
  const node = el(fakeDocument, 'a', '<script>alert(1)</script>', { href: `${base}/pull/1`, rel: 'noopener noreferrer' });
  assert.equal(node.textContent, '<script>alert(1)</script>');
  assert.equal(node.attrs.rel, 'noopener noreferrer');
  assert.throws(() => el(fakeDocument, 'a', 'Unsafe', { onclick: 'alert(1)' }), /Unsupported attribute/);
  assert.throws(() => el(fakeDocument, 'p', 'Unsafe', { style: 'color:red' }), /Unsupported attribute/);
});

test('all public JS and HTML avoid unsafe HTML rendering APIs', async () => {
  const root = new URL('../public/', import.meta.url);
  const files = await readdir(root, { recursive: true });
  const pattern = /innerHTML|outerHTML|insertAdjacentHTML|document\s*\.\s*write/;
  for (const name of files.filter((name) => /\.(js|html)$/.test(name))) {
    const text = await readFile(new URL(name, root), 'utf8');
    assert.equal(pattern.test(text), false, `${name} contains an unsafe rendering API`);
  }
});
