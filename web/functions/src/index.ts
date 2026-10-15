import { createHash } from 'node:crypto';
import { getApps, initializeApp } from 'firebase-admin/app';
import { getFirestore, type Firestore } from 'firebase-admin/firestore';
import { logger } from 'firebase-functions';
import { onCall, HttpsError, type CallableOptions, type CallableRequest } from 'firebase-functions/v2/https';
import { checkAuth, evaluateDecision, validateInput, type DecisionRequest } from './decision';
import { consumeRateLimit } from './rateLimit';

export function optionsFor(env: Record<string, string | undefined>): CallableOptions {
  return {
    region: 'us-central1',
    enforceAppCheck: env.ENFORCE_APP_CHECK !== 'false',
    maxInstances: 2,
    memory: '256MiB',
    timeoutSeconds: 30,
    // serviceAccount: 'approval-runtime@REPLACE_WITH_PROJECT_ID.iam.gserviceaccount.com',
  };
}

// Deployment options are captured once at module load, as required by onCall.
const deploymentOptions = optionsFor(process.env);
export function functionOptions(): CallableOptions {
  return { ...deploymentOptions };
}

interface Dependencies {
  now: () => Date;
  db: Firestore;
  allowedEmail: string | undefined;
}

function database(): Firestore {
  if (getApps().length === 0) initializeApp();
  return getFirestore();
}

export async function handleSubmitDecision(
  request: Pick<CallableRequest<unknown>, 'auth' | 'data'>,
  deps?: Dependencies,
): Promise<{ ok: true; eventId: string; status: 'queued' }> {
  const auth = checkAuth(request.auth, deps ? deps.allowedEmail : process.env.ALLOWED_EMAIL);
  if (!auth.ok) throw new HttpsError(auth.code, auth.message, auth.details);
  const validated = validateInput(request.data);
  if (!validated.ok) throw new HttpsError(validated.code, validated.message);
  const input = validated.input;
  // Initialize Admin only after rejecting unauthorized callers and invalid inputs.
  const db = deps?.db ?? database();
  const now = deps?.now() ?? new Date();
  try {
    await consumeRateLimit(db, auth.uid, now);
    const ref = db.collection('approval_requests').doc(input.requestId);
    const eventRef = ref.collection('events').doc();
    const emailHash = createHash('sha256').update(auth.email).digest('hex').slice(0, 16);
    await db.runTransaction(async (transaction) => {
      const snapshot = await transaction.get(ref);
      if (!snapshot.exists) throw new HttpsError('not-found', 'This request was not found.');
      const result = evaluateDecision({ request: snapshot.data() as DecisionRequest, input, now, uid: auth.uid });
      if (!result.ok) throw new HttpsError(result.code, result.message, result.details);
      transaction.create(eventRef, { ...result.event, emailHash });
      transaction.update(ref, {
        pendingWebDecision: { ...result.pendingWebDecision, eventId: eventRef.id },
        webUpdatedAt: now,
      });
    });
    logger.info('submitDecision', { requestId: input.requestId, decision: input.decision, outcome: 'queued' });
    return { ok: true, eventId: eventRef.id, status: 'queued' };
  } catch (error) {
    logger.info('submitDecision', {
      requestId: input.requestId, decision: input.decision,
      outcome: error instanceof HttpsError ? error.code : 'internal',
    });
    if (error instanceof HttpsError) throw error;
    throw new HttpsError('internal', 'The answer could not be sent. Please try again.');
  }
}

export const submitDecision = onCall(functionOptions(), (request) => handleSubmitDecision(request));
