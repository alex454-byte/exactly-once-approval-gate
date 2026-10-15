import type { Firestore } from 'firebase-admin/firestore';
import { HttpsError } from 'firebase-functions/v2/https';

export async function consumeRateLimit(db: Firestore, uid: string, now: Date): Promise<void> {
  const ref = db.collection('rate_limits').doc(uid);
  const time = now.getTime();
  await db.runTransaction(async (transaction) => {
    const snapshot = await transaction.get(ref);
    const stored: unknown = snapshot.data()?.calls;
    const calls: number[] = Array.isArray(stored)
      ? stored.filter((at): at is number => typeof at === 'number' && Number.isSafeInteger(at) && at > time - 3_600_000)
      : [];
    if (calls.length >= 60 || calls.filter((at) => at > time - 60_000).length >= 10) {
      throw new HttpsError('resource-exhausted', 'Too many answers. Wait a moment and try again.');
    }
    transaction.set(ref, { calls: [...calls, time], updatedAt: now });
  });
}
