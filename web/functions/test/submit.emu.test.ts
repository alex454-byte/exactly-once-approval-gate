import { createHash } from 'node:crypto';
import { afterAll, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { initializeApp, deleteApp, type App } from 'firebase-admin/app';
import { getFirestore, Timestamp, type Firestore } from 'firebase-admin/firestore';
import type { CallableRequest } from 'firebase-functions/v2/https';
import { handleSubmitDecision, functionOptions, optionsFor } from '../src/index';

const email = 'owner@example.test';
const id = 'abcdefghijklmnopqrstuv';
const time = new Date('2026-10-06T12:00:00Z');
let app: App;
let db: Firestore;
let now: Date;

beforeAll(() => {
  if (!process.env.FIRESTORE_EMULATOR_HOST) throw new Error('FIRESTORE_EMULATOR_HOST is required. Run npm run test:emu from the repository root.');
  app = initializeApp({ projectId: 'demo-approval-web' }, 'submit-tests');
  db = getFirestore(app);
});
beforeEach(async () => {
  await db.recursiveDelete(db.collection('approval_requests'));
  await db.recursiveDelete(db.collection('rate_limits'));
  now = new Date(time);
}, 30_000);
afterAll(async () => { if (app) await deleteApp(app); });

const deps = () => ({ db, now: () => now, allowedEmail: email });
function callable(data: unknown = { requestId: id, decision: 'yes', expectedVersion: 0 }, tokenOverrides: Record<string, unknown> = {}): CallableRequest {
  return {
    auth: { uid: 'owner', token: { email, email_verified: true, firebase: { sign_in_provider: 'google.com' }, ...tokenOverrides } },
    data, rawRequest: {}, acceptsStreaming: false,
  } as CallableRequest;
}
function requestRef() { return db.collection('approval_requests').doc(id); }
async function seed(overrides: Record<string, unknown> = {}) {
  const data = {
    kind: 'tester-upload', test: false, buildNumberExpected: 124, lastUploadedBuild: 123, localBuildMax: 123,
    appCommits: 2, sha: 'a'.repeat(40), prs: [], status: 'pending', version: 0,
    createdAt: time, updatedAt: time, expiresAt: new Date(time.getTime() + 43_200_000),
    decidedAt: null, decidedReason: null, history: [], pendingWebDecision: null, ...overrides,
  };
  await requestRef().set(data);
}
const submit = (data?: unknown) => handleSubmitDecision(callable(data), deps());
const events = () => requestRef().collection('events').get();

describe('submitDecision integration', () => {
  it('rejects non-allowlisted callers without touching Firestore', async () => {
    await seed();
    await expect(handleSubmitDecision(callable(undefined, { email: 'other@example.test' }), deps())).rejects.toMatchObject({ code: 'permission-denied' });
    expect((await events()).size).toBe(0);
    expect((await db.collection('rate_limits').get()).empty).toBe(true);
    expect((await requestRef().get()).data()?.pendingWebDecision).toBeNull();
  });
  it('rejects missing auth, missing configuration, and invalid input before rate limiting', async () => {
    const anonymous = callable();
    delete anonymous.auth;
    await expect(handleSubmitDecision(anonymous, deps())).rejects.toMatchObject({ code: 'unauthenticated' });
    await expect(handleSubmitDecision(callable(), { ...deps(), allowedEmail: undefined })).rejects.toMatchObject({ code: 'failed-precondition' });
    await expect(submit({ requestId: id, decision: 'YES', expectedVersion: 0 })).rejects.toMatchObject({ code: 'invalid-argument' });
    expect((await db.collection('rate_limits').get()).empty).toBe(true);
  });
  it('rejects a stale version without writing an event', async () => {
    await seed({ version: 2 });
    await expect(submit()).rejects.toMatchObject({ code: 'failed-precondition', details: { reason: 'stale', currentVersion: 2 } });
    expect((await events()).size).toBe(0);
  });
  it('rejects an expired pending request at equality', async () => {
    await seed({ expiresAt: time });
    await expect(submit()).rejects.toMatchObject({ code: 'failed-precondition', details: { reason: 'expired' } });
    expect((await events()).size).toBe(0);
  });
  it('creates exactly one event and matching envelope without changing the Mac-owned fields', async () => {
    await seed();
    const original = (await requestRef().get()).data();
    const response = await handleSubmitDecision(callable(undefined, { email: 'OWNER@EXAMPLE.TEST' }), deps());
    expect(response).toEqual({ ok: true, eventId: expect.any(String), status: 'queued' });
    const written = await events();
    expect(written.size).toBe(1);
    expect(written.docs[0].id).toBe(response.eventId);
    const event = written.docs[0].data();
    const emailHash = createHash('sha256').update(email).digest('hex').slice(0, 16);
    expect(event).toEqual({ type: 'web_decision', decision: 'yes', expectedVersion: 0, at: Timestamp.fromDate(time), uid: 'owner', emailHash });
    expect(emailHash).toMatch(/^[a-f0-9]{16}$/);
    expect(JSON.stringify(event).toLowerCase()).not.toContain(email);
    const after = (await requestRef().get()).data();
    expect(after).toEqual({
      ...original, webUpdatedAt: Timestamp.fromDate(time),
      pendingWebDecision: { decision: 'yes', expectedVersion: 0, eventId: response.eventId, at: Timestamp.fromDate(time), uid: 'owner' },
    });
  });
  it('rejects the second submit for the same version as in-flight', async () => {
    await seed();
    await submit();
    await expect(submit({ requestId: id, decision: 'no', expectedVersion: 0 })).rejects.toMatchObject({ code: 'failed-precondition', details: { reason: 'in-flight' } });
    expect((await events()).size).toBe(1);
  });
  it('serializes simultaneous answers so only one queues', async () => {
    await seed();
    const results = await Promise.allSettled([submit(), submit({ requestId: id, decision: 'no', expectedVersion: 0 })]);
    expect(results.filter((result) => result.status === 'fulfilled')).toHaveLength(1);
    const failure = results.find((result) => result.status === 'rejected');
    expect(failure?.status === 'rejected' && failure.reason).toMatchObject({ code: 'failed-precondition', details: { reason: 'in-flight' } });
    expect((await events()).size).toBe(1);
  }, 20_000);
  it('allows change to No after an approved request expires', async () => {
    await seed({ status: 'approved', expiresAt: new Date(time.getTime() - 1) });
    await expect(submit({ requestId: id, decision: 'no', expectedVersion: 0 })).resolves.toMatchObject({ ok: true, status: 'queued' });
    expect((await requestRef().get()).data()?.status).toBe('approved');
  });
  it('rejects a missing request', async () => {
    await expect(submit()).rejects.toMatchObject({ code: 'not-found' });
    expect((await events()).size).toBe(0);
  });
  it('rejects call 11 within the minute and allows again exactly after the rolling window', async () => {
    // Valid inputs count even when the requested document is missing.
    for (let i = 0; i < 10; i++) await expect(submit()).rejects.toMatchObject({ code: 'not-found' });
    await expect(submit()).rejects.toMatchObject({ code: 'resource-exhausted' });
    expect((await db.collection('rate_limits').doc('owner').get()).data()?.calls).toHaveLength(10);
    now = new Date(time.getTime() + 59_999);
    await expect(submit()).rejects.toMatchObject({ code: 'resource-exhausted' });
    now = new Date(time.getTime() + 60_000);
    await seed();
    await expect(submit()).resolves.toMatchObject({ ok: true });
  }, 20_000);
  it('enforces the rolling hourly cap across minutes and releases the oldest calls at one hour', async () => {
    for (let minute = 0; minute < 6; minute++) {
      now = new Date(time.getTime() + minute * 60_000);
      for (let call = 0; call < 10; call++) await expect(submit()).rejects.toMatchObject({ code: 'not-found' });
    }
    now = new Date(time.getTime() + 6 * 60_000);
    await expect(submit()).rejects.toMatchObject({ code: 'resource-exhausted' });
    now = new Date(time.getTime() + 3_600_000);
    await seed();
    await expect(submit()).resolves.toMatchObject({ ok: true });
    expect((await db.collection('rate_limits').doc('owner').get()).data()?.calls).toHaveLength(51);
  }, 30_000);
  it('sets the callable deployment options with App Check enabled unless explicitly disabled', () => {
    expect(optionsFor({}).enforceAppCheck).toBe(true);
    expect(optionsFor({ ENFORCE_APP_CHECK: 'false' }).enforceAppCheck).toBe(false);
    expect(optionsFor({ ENFORCE_APP_CHECK: 'False' }).enforceAppCheck).toBe(true);
    expect(functionOptions()).toMatchObject({ region: 'us-central1', maxInstances: 2, memory: '256MiB', timeoutSeconds: 30 });
    expect(functionOptions().enforceAppCheck).toBe(process.env.ENFORCE_APP_CHECK !== 'false');
  });
});
