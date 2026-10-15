# Mac ↔ Firebase contract

This project only records the owner's web answers. It never builds, uploads, starts a process, or schedules a build. The Mac daemon's **local record and state machine are authoritative**. A queued web answer is not an approval until the Mac applies it. Telegram and web answers compete using the same local version.

Use one Firebase project and its default Firestore database. All timestamps below are Firestore `Timestamp` values, representing UTC instants; the page displays local time. Integers must be JavaScript-safe integers. Request IDs are exactly 22 characters matching `/^[A-Za-z0-9_-]{22}$/`, as produced by `secrets.token_urlsafe(16)`.

## Request documents

Path: `approval_requests/{requestId}`. The Mac publishes a new request with **`set()` without merge**, and creates an event with `type: "system"`, `status: "pending"`, `reason: "created"`, `version: 0`, and `at: createdAt`. Prefer a single write batch for the initial document and event. Never reuse an ID or repeat the initial `set()` over an existing request.

| Field | Type | Writer and meaning |
| --- | --- | --- |
| `kind` | `"tester-upload" \| "test-echo"` | Mac. The page prefixes test-echo requests with `[TEST]`. |
| `test` | boolean | Mac. Also causes the `[TEST]` prefix when true. |
| `buildNumberExpected` | integer | Mac. Expected tester build number. |
| `lastUploadedBuild` | integer | Mac. Last tester build uploaded. |
| `localBuildMax` | integer or null | Mac. Highest known local build number. |
| `appCommits` | integer | Mac. App commit count. |
| `sha` | string, exactly 40 hexadecimal characters | Mac. Full commit SHA. |
| `prs` | array of PR objects | Mac. May be empty; schema below. |
| `status` | `"pending" \| "approved" \| "declined" \| "cancel_requested" \| "expired" \| "finished"` | Mac only, including after a web answer is applied. |
| `version` | non-negative integer | Mac only. Starts at 0; increments by 1 on **every state transition**. |
| `createdAt` | Timestamp | Mac. Request creation instant. |
| `expiresAt` | Timestamp | Mac. Exactly 12 hours after `createdAt`. |
| `updatedAt` | Timestamp | Mac. Last authoritative update. |
| `decidedAt` | Timestamp or null | Mac. Local state machine's latest decision time; null before a decision. |
| `decidedReason` | string or null | Mac. Local decision reason; null before a decision. |
| `history` | array of history entries | Mac only. Ordered transition history, including the initial version-0 `created` entry. |
| `pendingWebDecision` | null or decision envelope | Function creates/replaces the envelope. Mac initializes it to null and **only clears it conditionally**, as described below. |
| `webUpdatedAt` | Timestamp, absent before the first web answer | Function only. Time the latest web answer was queued; not an authoritative decision time. |

The Mac's existing snake_case payload maps directly: `build_number_expected` → `buildNumberExpected`, `last_uploaded_build` → `lastUploadedBuild`, `local_build_max` → `localBuildMax`, `app_commits` → `appCommits`. It enriches PR entries with URLs and linked issues. No personal data, Telegram chat IDs, tokens, or Telegram user IDs may be published anywhere in these documents or events. The `uid` fields below are **Firebase Auth UIDs**, not Telegram identifiers.

| PR field | Type | Writer |
| --- | --- | --- |
| `number` | positive integer | Mac |
| `title` | string | Mac |
| `url` | string | Mac |
| `issues` | array of linked issue objects, possibly empty | Mac |

| Linked issue field | Type | Writer |
| --- | --- | --- |
| `number` | positive integer | Mac |
| `title` | string or null | Mac |
| `url` | string | Mac |

The page only links exact `https://github.com/example-org/Example-App/pull/<number>` and `https://github.com/example-org/Example-App/issues/<number>` URLs with numbers 1–9999999 and no leading zeros. The URL's kind and number must match the displayed record. All other URLs are withheld; titles remain plain text.

| History field | Type | Writer |
| --- | --- | --- |
| `version` | non-negative integer | Mac; resulting local version |
| `status` | one of the request statuses | Mac; resulting local status |
| `at` | Timestamp | Mac; transition time |
| `reason` | string | Mac; human-readable reason |
| `via` | `"telegram" \| "web" \| "system"` | Mac; origin of transition |

| Decision envelope field | Type | Writer |
| --- | --- | --- |
| `decision` | `"yes" \| "no"` | Function |
| `expectedVersion` | non-negative integer | Function; version the browser answered |
| `eventId` | string | Function; ID of the corresponding `web_decision` event |
| `at` | Timestamp | Function; server handler time |
| `uid` | string | Function; authenticated Firebase UID |

The Mac never constructs or alters an envelope's contents. It must omit `pendingWebDecision` and `webUpdatedAt` from routine status updates so it cannot overwrite a concurrent web answer. Clearing the specific envelope it processed is the sole exception to the function's ownership of that field.

## Append-only events

Path: `approval_requests/{requestId}/events/{eventId}`. Use `create()` for new events. **Events are never updated or deleted by either application.** The callable uses an auto-generated Firestore document ID. Every event has only the fields for its type, listed below.

| Web submission field | Type | Writer |
| --- | --- | --- |
| `type` | `"web_decision"` | Function |
| `decision` | `"yes" \| "no"` | Function |
| `expectedVersion` | non-negative integer | Function |
| `at` | Timestamp | Function |
| `uid` | string (Firebase Auth UID) | Function |
| `emailHash` | string, 16 lowercase hex characters | Function; first 16 hex characters of SHA-256 of the lowercased authenticated email (UTF-8). **Never the raw email.** |

| Web acknowledgement field | Type | Writer |
| --- | --- | --- |
| `type` | `"web_decision_applied" \| "web_decision_rejected"` | Mac |
| `sourceEventId` | string | Mac; original web event ID |
| `reason` | string | Mac; use exactly `"superseded"` on a local version mismatch |
| `resultingVersion` | integer or null | Mac; resulting local version for applied, null for rejected |
| `at` | Timestamp | Mac; acknowledgement time |

| Mirrored transition field | Type | Writer |
| --- | --- | --- |
| `type` | `"telegram_decision" \| "system"` | Mac |
| `status` | one of the request statuses | Mac |
| `reason` | string | Mac; `"created"` for initial publication |
| `version` | non-negative integer | Mac |
| `at` | Timestamp | Mac |

Web transitions are recorded in `history` with `via: "web"` and acknowledged with `web_decision_applied`; a separate Telegram event is not needed. The page merges history and events by time and collapses exact mirrored Telegram/system transitions. It still shows both the submitted web answer and its acknowledgement.

## Callable and arbitration

Callable name: `submitDecision`, region `us-central1`. Input must be a plain object with **exactly** `requestId`, `decision`, and `expectedVersion`. Output is `{ok: true, eventId: string, status: "queued"}`. No client writes to Firestore directly.

After verifying the allowlisted, verified Google identity and validating input, the function consumes a per-UID rate-limit slot. It allows at most 10 calls in a rolling 60 seconds and 60 in a rolling hour. All authorized, syntactically valid calls count, including stale/missing requests. Rejected rate-limit attempts do not extend the window. `rate_limits/{uid}` contains `calls` (array of integer epoch milliseconds) and `updatedAt` (Timestamp), written only by the function; clients cannot read or write it. Old entries are pruned on the next call.

Within one transaction the function checks existence, version equality, expiry, permitted action, then same-version in-flight status, in that order. It creates the event and writes the envelope plus `webUpdatedAt` atomically. It **does not** change `status`, `version`, history, or decision timestamps. An envelope for an older version can be replaced. Callable App Check enforcement happens before the inner handler; invalid App Check can therefore reject a call before its auth/input checks.

| Local/published status | Allowed web action before expiry | At or after `expiresAt` |
| --- | --- | --- |
| `pending` | yes or no | neither; expired |
| `approved` | no | no still allowed |
| `declined` | yes | neither; expired |
| `cancel_requested`, `expired`, `finished` | none | none |

Errors use callable codes: `unauthenticated`, `permission-denied`, `invalid-argument`, `resource-exhausted`, `not-found`, or `failed-precondition`. State failures include `details.reason`: `stale` (also `currentVersion`), `expired`, `not-allowed`, or `in-flight`. Missing or placeholder server configuration fails closed with `failed-precondition`. Unknown server failures are returned as a generic `internal` error; the app never displays raw errors.

## Mac consumption protocol

1. Listen or poll for `approval_requests` documents where `pendingWebDecision != null`. A single-field query is sufficient; no composite index is needed. Capture the complete envelope and its `eventId`.
2. Serialize processing for that request with Telegram callbacks and local system transitions. Load the **local** record. Check a durable local ledger keyed by `eventId` first, so a retry after a crash cannot apply the same answer twice. Missing local records must be rejected, never synthesized from Firestore.
3. If the local version differs from `expectedVersion`, record `web_decision_rejected` with reason **`"superseded"`** and `resultingVersion: null`. Otherwise feed `decision` and `expectedVersion` into the same local state machine used by Telegram callbacks. Recheck expiry, launch state, and all existing prechecks locally. Map actions exactly:
   - yes on pending: approve with the usual **2-minute grace**;
   - no on pending: decline;
   - no on approved: change to No, or `cancel_requested` if already launched;
   - yes on declined: change to Yes, **re-running the precheck**;
   - other states or expired pending/declined: reject; approved → no remains allowed after expiry.
4. Durably record the local outcome and event ID with the local transition. Firestore cannot atomically commit with local disk, so retries must resume publication of that recorded outcome instead of repeating the transition or incorrectly rejecting it as superseded.
5. In **one Firestore transaction**, read the current request, append the acknowledgement event, publish local `status`, `version`, `history`, `decidedAt`, `decidedReason`, and `updatedAt`, and clear `pendingWebDecision` to null **only if its current `eventId` still equals the processed event ID**. If another envelope replaced it, leave that envelope untouched. Never write `webUpdatedAt`. Use a stable acknowledgement ID, such as `ack_<sourceEventId>`, and detect an already-created acknowledgement on retry; never update it. Serialize publication and never replay an older local snapshot over a newer version. Rejections also reconcile the published state to the current local state without manufacturing a transition.
6. Send the usual Telegram confirmation, marked **“via web”**, after processing. Use the ledger to avoid duplicate confirmation sends on ordinary retries.
7. Publish **every** Telegram-driven and system transition, including expiry sweeps, launch/cancellation changes, and `finished`, updating the request and appending its mirrored event together. Increment version once per local transition. These writes must preserve any outstanding envelope until its consumer conditionally acknowledges it.

A successful function call means only that an answer was queued. The browser shows “waiting for the Mac” while the current-version envelope exists. A Telegram answer can supersede it before the Mac consumes it; this is expected, and the rejection appears in the decision history. If the Mac is offline, the envelope persists until processing resumes.

Telegram request link: `https://<hosting-domain>/r/<requestId>`.

## IAM and custody

Create the dedicated publisher service account `approval-publisher@<project>.iam.gserviceaccount.com` and grant it **only `roles/datastore.user` on the project**. Do not grant Owner, Editor, or Firebase Admin. This role supplies the Firestore data access the publisher needs.

The Admin SDK **bypasses Firestore security rules**. Mac-side protection comes from IAM and key custody, not the email allowlist. Firestore IAM cannot be scoped per collection; this account can read/write other Firestore collections in the same project, including rate limits. Prefer a project dedicated to these approvals. Append-only events and field ownership are application requirements, not restrictions enforced against an Admin credential by the rules.

If using a JSON service-account key, store it outside every repository, `chmod 600` it, point the daemon's `GOOGLE_APPLICATION_CREDENTIALS` at that file, never commit it, and rotate/revoke it on a schedule and immediately if exposed. The organization policy `iam.disableServiceAccountKeyCreation` may block key creation. Use an org-approved keyless alternative: Workload Identity Federation for a suitable trusted external identity, service-account impersonation with short-lived credentials, or running the publisher on a Google Cloud workload with an attached service account. Coordinate the identity and token permissions with the org administrator; do not weaken the org policy to make a key.

For the function, prefer a **separate runtime service account**, for example `approval-runtime@<project>.iam.gserviceaccount.com`, with `roles/datastore.user`. Set it in the commented `serviceAccount` option in `functions/src/index.ts`. The option is intentionally commented so the platform default works initially; ensure that default runtime identity has the necessary Firestore permission. Keep deployment administration separate from runtime and publisher identities.
