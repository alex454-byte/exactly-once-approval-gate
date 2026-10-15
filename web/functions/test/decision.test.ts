import { describe, expect, it } from 'vitest';
import { checkAuth, validateInput, evaluateDecision, type DecisionInput, type DecisionRequest } from '../src/decision';
import { optionsFor, functionOptions } from '../src/index';

const id = 'abcdefghijklmnopqrstuv';
const email = 'owner@example.test';
const auth = (overrides: Record<string, unknown> = {}) => ({
  uid: 'owner', token: { email, email_verified: true, firebase: { sign_in_provider: 'google.com' }, ...overrides },
});
const input: DecisionInput = { requestId: id, decision: 'yes', expectedVersion: 2 };
const now = new Date('2026-10-06T12:00:00Z');
const baseRequest: DecisionRequest = { status: 'pending', version: 2, expiresAt: new Date(now.getTime() + 1), pendingWebDecision: null };
const evaluate = (request: Partial<DecisionRequest> = {}, change: Partial<DecisionInput> = {}) =>
  evaluateDecision({ request: { ...baseRequest, ...request }, input: { ...input, ...change }, now, uid: 'owner' });

describe('checkAuth', () => {
  it('requires authentication', () => {
    expect(checkAuth(undefined, email)).toMatchObject({ ok: false, code: 'unauthenticated' });
    expect(checkAuth(null, email)).toMatchObject({ ok: false, code: 'unauthenticated' });
  });
  it.each([
    { email: 'other@example.test' }, { email: null }, { email_verified: false }, { email_verified: 'true' },
    { firebase: { sign_in_provider: 'password' } }, { firebase: { sign_in_provider: 'phone' } },
    { firebase: { sign_in_provider: 'anonymous' } }, { firebase: undefined },
  ])('denies an unauthorized identity: %j', (token) => {
    expect(checkAuth(auth(token), email)).toMatchObject({ ok: false, code: 'permission-denied' });
  });
  it('allows a verified Google identity and compares email without case sensitivity', () => {
    expect(checkAuth(auth({ email: 'OWNER@EXAMPLE.TEST' }), 'Owner@Example.Test')).toEqual({ ok: true, uid: 'owner', email });
  });
  it('allows a matching account when allowedEmail has surrounding whitespace', () => {
    expect(checkAuth(auth(), ' \tOwner@Example.Test\n ')).toEqual({ ok: true, uid: 'owner', email });
  });
  it.each([undefined, '', ' ', 'REPLACE_WITH_OWNER_EMAIL', 'prefixREPLACE_email', 'owner@example.invalid', 'OWNER@EXAMPLE.INVALID'])('fails closed with allowedEmail=%s', (allowed) => {
    expect(checkAuth(auth(), allowed)).toMatchObject({ ok: false, code: 'failed-precondition' });
  });
});

describe('validateInput', () => {
  it('accepts exactly the three fields', () => expect(validateInput(input)).toEqual({ ok: true, input }));
  it.each(['short', id + 'x', id + '\n', '!' + id.slice(1), '../' + id.slice(3), null])('rejects ID %s', (requestId) => {
    expect(validateInput({ ...input, requestId })).toMatchObject({ ok: false, code: 'invalid-argument' });
  });
  it.each(['YES', 'approve', '', null])('rejects decision %s', (decision) => {
    expect(validateInput({ ...input, decision })).toMatchObject({ ok: false, code: 'invalid-argument' });
  });
  it.each([-1, 1.5, '1', NaN, 2 ** 53, Infinity, null])('rejects version %s', (expectedVersion) => {
    expect(validateInput({ ...input, expectedVersion })).toMatchObject({ ok: false, code: 'invalid-argument' });
  });
  it.each([null, undefined, [], 'input', 1, new Date(), {}, { ...input, extra: true }, { ...input, [Symbol('extra')]: true }])('rejects non-plain or wrong-shaped data: %j', (data) => {
    expect(validateInput(data)).toMatchObject({ ok: false, code: 'invalid-argument' });
  });
  it('accepts version zero and the largest safe integer', () => {
    expect(validateInput({ ...input, expectedVersion: 0 }).ok).toBe(true);
    expect(validateInput({ ...input, expectedVersion: Number.MAX_SAFE_INTEGER }).ok).toBe(true);
  });
});

describe('evaluateDecision', () => {
  it.each([1, 3])('rejects a stale version on either side: %s', (expectedVersion) => {
    expect(evaluate({}, { expectedVersion })).toMatchObject({ ok: false, code: 'failed-precondition', details: { reason: 'stale', currentVersion: 2 } });
  });
  it.each(['pending', 'declined'])('expires %s exactly at the expiry boundary and thereafter', (status) => {
    for (const expiresAt of [now, new Date(now.getTime() - 1)]) {
      expect(evaluate({ status, expiresAt })).toMatchObject({ ok: false, details: { reason: 'expired' } });
    }
  });
  it('allows approved + no after expiry', () => {
    expect(evaluate({ status: 'approved', expiresAt: new Date(now.getTime() - 1) }, { decision: 'no' }).ok).toBe(true);
  });
  it.each(['finished', 'expired', 'cancel_requested'])('rejects both answers for %s', (status) => {
    for (const decision of ['yes', 'no'] as const) {
      expect(evaluate({ status }, { decision })).toMatchObject({ ok: false, details: { reason: 'not-allowed' } });
    }
  });
  it('rejects approved + yes and declined + no', () => {
    expect(evaluate({ status: 'approved' })).toMatchObject({ ok: false, details: { reason: 'not-allowed' } });
    expect(evaluate({ status: 'declined' }, { decision: 'no' })).toMatchObject({ ok: false, details: { reason: 'not-allowed' } });
  });
  it('rejects a same-version in-flight decision', () => {
    expect(evaluate({ pendingWebDecision: { expectedVersion: 2 } })).toMatchObject({ ok: false, details: { reason: 'in-flight' } });
  });
  it('can replace an older pending envelope', () => {
    expect(evaluate({ pendingWebDecision: { expectedVersion: 1 } }).ok).toBe(true);
  });
  it.each([
    ['pending', 'yes'], ['pending', 'no'], ['approved', 'no'], ['declined', 'yes'],
  ] as const)('returns exact event and envelope drafts for %s + %s', (status, decision) => {
    expect(evaluate({ status }, { decision })).toEqual({
      ok: true,
      event: { type: 'web_decision', decision, expectedVersion: 2, at: now, uid: 'owner' },
      pendingWebDecision: { decision, expectedVersion: 2, at: now, uid: 'owner' },
    });
  });
  it('does not mutate its inputs or advance Mac-owned state', () => {
    const request = Object.freeze({ ...baseRequest, expiresAt: { toMillis: () => now.getTime() + 1 } });
    expect(evaluateDecision({ request, input: Object.freeze({ ...input }), now, uid: 'owner' }).ok).toBe(true);
    expect(request.status).toBe('pending');
    expect(request.version).toBe(2);
  });
  it('checks stale, expiry, action, and in-flight in order', () => {
    expect(evaluate({ expiresAt: now, pendingWebDecision: { expectedVersion: 2 } }, { expectedVersion: 1 })).toMatchObject({ details: { reason: 'stale' } });
    expect(evaluate({ expiresAt: now, pendingWebDecision: { expectedVersion: 2 } })).toMatchObject({ details: { reason: 'expired' } });
    expect(evaluate({ status: 'finished', pendingWebDecision: { expectedVersion: 2 } })).toMatchObject({ details: { reason: 'not-allowed' } });
  });
});

describe('deployment options', () => {
  it('enforces App Check by default, with only an exact false opt-out', () => {
    expect(optionsFor({})).toMatchObject({ enforceAppCheck: true, region: 'us-central1', maxInstances: 2, memory: '256MiB', timeoutSeconds: 30 });
    for (const value of ['', 'true', 'FALSE', '0', ' false ']) expect(optionsFor({ ENFORCE_APP_CHECK: value }).enforceAppCheck).toBe(true);
    expect(optionsFor({ ENFORCE_APP_CHECK: 'false' }).enforceAppCheck).toBe(false);
    expect(functionOptions().enforceAppCheck).toBe(process.env.ENFORCE_APP_CHECK !== 'false');
  });
});
