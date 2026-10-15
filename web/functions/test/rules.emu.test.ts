import { readFile } from 'node:fs/promises';
import { afterAll, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { initializeTestEnvironment, assertSucceeds, assertFails, type RulesTestEnvironment } from '@firebase/rules-unit-testing';
import { collection, deleteDoc, doc, getDoc, getDocs, limit, orderBy, query, setDoc, updateDoc } from 'firebase/firestore';

const projectId = 'demo-approval-web';
const email = 'owner@example.test';
const id = 'abcdefghijklmnopqrstuv';
let env: RulesTestEnvironment;

beforeAll(async () => {
  const address = process.env.FIRESTORE_EMULATOR_HOST;
  if (!address) throw new Error('FIRESTORE_EMULATOR_HOST is required. Run npm run test:emu from the repository root.');
  const split = address.lastIndexOf(':');
  const rules = (await readFile(new URL('../../firestore.rules.template', import.meta.url), 'utf8')).replaceAll('__ALLOWED_EMAIL__', email);
  env = await initializeTestEnvironment({
    projectId, firestore: { rules, host: address.slice(0, split), port: Number(address.slice(split + 1)) },
  });
}, 30_000);

beforeEach(async () => {
  await env.clearFirestore();
  await env.withSecurityRulesDisabled(async (context) => {
    const db = context.firestore();
    await setDoc(doc(db, 'approval_requests', id), { status: 'pending', version: 0, createdAt: new Date(), pendingWebDecision: null });
    await setDoc(doc(db, 'approval_requests', id, 'events', 'created'), { type: 'system', reason: 'created', at: new Date(), version: 0, status: 'pending' });
    await setDoc(doc(db, 'rate_limits', 'owner'), { calls: [Date.now()] });
  });
}, 30_000);

afterAll(async () => { if (env) await env.cleanup(); });

function owner(overrides: Record<string, unknown> = {}) {
  return env.authenticatedContext('owner', {
    email, email_verified: true, firebase: { sign_in_provider: 'google.com' }, ...overrides,
  }).firestore();
}

describe('read allowlist', () => {
  it('lets the verified Google owner get and list both requests and events', async () => {
    const db = owner();
    expect((await assertSucceeds(getDoc(doc(db, 'approval_requests', id)))).exists()).toBe(true);
    expect((await assertSucceeds(getDocs(query(collection(db, 'approval_requests'), orderBy('createdAt', 'desc'), limit(50))))).size).toBe(1);
    expect((await assertSucceeds(getDoc(doc(db, 'approval_requests', id, 'events', 'created')))).exists()).toBe(true);
    expect((await assertSucceeds(getDocs(query(collection(db, 'approval_requests', id, 'events'), orderBy('at'))))).size).toBe(1);
  });
  it.each([
    ['unauthenticated', null],
    ['another email', { email: 'other@example.test' }],
    ['unverified', { email_verified: false }],
    ['password provider', { firebase: { sign_in_provider: 'password' } }],
    ['uppercase email (rules compare exactly)', { email: 'OWNER@EXAMPLE.TEST' }],
  ])('denies request and event reads for %s', async (_label, claims) => {
    const db = claims === null ? env.unauthenticatedContext().firestore() : owner(claims);
    await assertFails(getDoc(doc(db, 'approval_requests', id)));
    await assertFails(getDocs(collection(db, 'approval_requests')));
    await assertFails(getDoc(doc(db, 'approval_requests', id, 'events', 'created')));
    await assertFails(getDocs(collection(db, 'approval_requests', id, 'events')));
  });
});

describe('clients cannot write', () => {
  it('denies the owner create, update, delete, and envelope-only updates on requests', async () => {
    const db = owner();
    await assertFails(setDoc(doc(db, 'approval_requests', 'newrequest0000000000000'), { status: 'approved' }));
    await assertFails(updateDoc(doc(db, 'approval_requests', id), { status: 'approved', version: 1 }));
    await assertFails(updateDoc(doc(db, 'approval_requests', id), { pendingWebDecision: { decision: 'yes', expectedVersion: 0 } }));
    await assertFails(deleteDoc(doc(db, 'approval_requests', id)));
  });
  it('denies the owner create, update, and delete on events', async () => {
    const db = owner();
    await assertFails(setDoc(doc(db, 'approval_requests', id, 'events', 'new'), { type: 'web_decision' }));
    await assertFails(updateDoc(doc(db, 'approval_requests', id, 'events', 'created'), { reason: 'changed' }));
    await assertFails(deleteDoc(doc(db, 'approval_requests', id, 'events', 'created')));
  });
  it.each(['owner', 'other', 'anonymous'])('denies all rate_limits access to %s', async (identity) => {
    const db = identity === 'anonymous' ? env.unauthenticatedContext().firestore()
      : owner(identity === 'other' ? { email: 'other@example.test' } : {});
    await assertFails(getDoc(doc(db, 'rate_limits', 'owner')));
    await assertFails(getDocs(collection(db, 'rate_limits')));
    await assertFails(setDoc(doc(db, 'rate_limits', 'new'), { calls: [] }));
    await assertFails(updateDoc(doc(db, 'rate_limits', 'owner'), { calls: [] }));
    await assertFails(deleteDoc(doc(db, 'rate_limits', 'owner')));
  });
  it('denies unmatched collections even for the owner', async () => {
    const db = owner();
    await assertFails(getDoc(doc(db, 'other', 'document')));
    await assertFails(setDoc(doc(db, 'other', 'document'), { value: true }));
  });
});
