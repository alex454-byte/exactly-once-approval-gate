export type Decision = 'yes' | 'no';
export interface DecisionInput {
  requestId: string;
  decision: Decision;
  expectedVersion: number;
}
export interface AuthIdentity {
  uid: string;
  token: Record<string, unknown>;
}
export interface Failure {
  ok: false;
  code: 'unauthenticated' | 'permission-denied' | 'invalid-argument' | 'failed-precondition';
  message: string;
  details?: { reason: string; currentVersion?: number };
}
type Instant = Date | { toMillis(): number };
export interface DecisionRequest {
  status: string;
  version: number;
  expiresAt: Instant;
  pendingWebDecision?: { expectedVersion: number } | null;
}

export function checkAuth(auth: AuthIdentity | null | undefined, allowedEmail: string | undefined):
  Failure | { ok: true; uid: string; email: string } {
  if (!allowedEmail?.trim() || allowedEmail.includes('REPLACE_')
      || allowedEmail.trim().toLowerCase() === 'owner@example.invalid') {
    return { ok: false, code: 'failed-precondition', message: 'The approval service is not configured.' };
  }
  if (!auth) return { ok: false, code: 'unauthenticated', message: 'Sign in with Google to answer.' };
  const email = auth.token.email;
  const firebase = auth.token.firebase as { sign_in_provider?: unknown } | undefined;
  if (typeof email !== 'string' || email.toLowerCase() !== allowedEmail.trim().toLowerCase()
      || auth.token.email_verified !== true || firebase?.sign_in_provider !== 'google.com') {
    return { ok: false, code: 'permission-denied', message: 'This Google account is not allowed.' };
  }
  return { ok: true, uid: auth.uid, email: email.toLowerCase() };
}

export function validateInput(data: unknown): Failure | { ok: true; input: DecisionInput } {
  const invalid: Failure = { ok: false, code: 'invalid-argument', message: 'The answer is not valid.' };
  if (data === null || typeof data !== 'object' || Array.isArray(data)
      || ![Object.prototype, null].includes(Object.getPrototypeOf(data))) return invalid;
  const keys = Reflect.ownKeys(data);
  if (keys.length !== 3 || !['requestId', 'decision', 'expectedVersion'].every((key) => keys.includes(key))) return invalid;
  const { requestId, decision, expectedVersion } = data as Record<string, unknown>;
  if (typeof requestId !== 'string' || requestId.length !== 22 || !/^[A-Za-z0-9_-]{22}$/.test(requestId)
      || (decision !== 'yes' && decision !== 'no')
      || typeof expectedVersion !== 'number' || !Number.isSafeInteger(expectedVersion) || expectedVersion < 0) return invalid;
  return { ok: true, input: { requestId, decision, expectedVersion } };
}

export function evaluateDecision({ request, input, now, uid }: {
  request: DecisionRequest;
  input: DecisionInput;
  now: Date;
  uid: string;
}) {
  const rejected = (reason: string, message: string, currentVersion?: number): Failure => ({
    ok: false, code: 'failed-precondition', message,
    details: currentVersion === undefined ? { reason } : { reason, currentVersion },
  });
  if (request.version !== input.expectedVersion) {
    return rejected('stale', 'This request has changed. Refresh it and answer again.', request.version);
  }
  if (request.status === 'pending' || request.status === 'declined') {
    const expiry = request.expiresAt instanceof Date ? request.expiresAt.getTime() : request.expiresAt?.toMillis();
    if (!Number.isFinite(expiry) || now.getTime() >= expiry) return rejected('expired', 'This request has expired.');
  }
  const allowed = (request.status === 'pending' && (input.decision === 'yes' || input.decision === 'no'))
    || (request.status === 'approved' && input.decision === 'no')
    || (request.status === 'declined' && input.decision === 'yes');
  if (!allowed) return rejected('not-allowed', 'This request has already been handled.');
  if (request.pendingWebDecision != null && request.pendingWebDecision.expectedVersion === request.version) {
    return rejected('in-flight', 'Your answer is already waiting for the Mac.');
  }
  // Pure drafts: the handler adds the database event ID and the email hash.
  return {
    ok: true as const,
    event: { type: 'web_decision' as const, decision: input.decision, expectedVersion: input.expectedVersion, at: now, uid },
    pendingWebDecision: { decision: input.decision, expectedVersion: input.expectedVersion, at: now, uid },
  };
}
