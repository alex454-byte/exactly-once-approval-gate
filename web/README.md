# Web approvals (optional second channel)

A small mobile page for answering tester-upload requests already managed by your Mac daemon. Hosting serves static files; Google sign-in and Firestore rules restrict reading; one callable function queues a web answer. **Nothing here builds or uploads anything.** The Mac applies decisions using its existing local state machine. See [CONTRACT.md](CONTRACT.md) before implementing the publisher/consumer.

Requires Node.js 22, npm, the globally installed Firebase CLI, and Java 21 for Firestore emulator tests. This repository intentionally contains no real project credentials, installed dependencies, or generated browser/server config.

## Setup

1. **Create the project.** In Google Cloud Console, choose your organization in the organization selector if you use one, create a project, and attach a billing account. In Firebase Console choose **Add project**, select that existing Google Cloud project, and enable Firebase. Confirm the organization/billing association in Cloud Console. Select the **Blaze** plan (required for Cloud Functions). Note the project ID, not just the display name.

2. **Fill in local configuration.** In this directory:

   ```sh
   cp config.example.json config.local.json
   ```

   Fill in `projectId` and `allowedEmail`. In Firebase **Project settings → General → Your apps → Add app → Web**, register the app and copy the web config fields into `firebaseWebConfig`. Its `projectId` must match. Fill `appCheckSiteKey` when completing step 6. Keep `enforceAppCheck: true` normally. Replace every `REPLACE_` value; the renderer will reject any that remain. `config.local.json`, `public/config.js`, and `functions/.env` are ignored by Git. Firebase's web app config and the reCAPTCHA site key are public client identifiers, not Admin credentials. Never place a service-account key in this config or under `public/`.

   By default, set `firebaseWebConfig.authDomain` to the Hosting domain (`<project-id>.web.app`, or the custom domain if used) rather than `<project-id>.firebaseapp.com` so redirect sign-in works on mobile Safari and other browsers that block third-party storage. In **Google Cloud Console → APIs & Services → Credentials → Web client (auto created by Google Service)**, add `https://<that-domain>/__/auth/handler` to **Authorized redirect URIs**.

   The renderer lowercases `allowedEmail`. The Google account's email must match the rules **exactly** (Google account emails are lowercase); do not use an alias, alternate spelling, or different dotted address. The function also checks email without case sensitivity. Quotes and backslashes are rejected to keep the generated rules safe.

3. **Select the Firebase project.** Replace `REPLACE_WITH_PROJECT_ID` in `.firebaserc`, or run `firebase use --add` and select this project. Deployment commands below also pass `--project` explicitly. The renderer does not edit `.firebaserc`.

4. **Enable Google authentication.** In **Authentication → Sign-in method**, enable **Google only**. Set the support email as requested by the console. Add your Hosting domain(s) in **Authentication → Settings → Authorized domains**. Enable email enumeration protection in Authentication's settings if it is not already enabled. Google sign-up cannot simply be disabled while allowing first-time Google sign-in: the first sign-in creates a Firebase Auth user. The Firestore rules and callable email allowlist are the access gate, even if another Google account creates an Auth user. No allowlisted email is sent to the browser configuration or shown in denial messages.

5. **Create Firestore.** In **Firestore Database → Create database**, create the default database in **Native mode**, choose its region deliberately, and start in production/locked mode. The callable runs in `us-central1`; selecting a nearby Firestore location avoids unnecessary latency. Deploy the rendered rules in step 8 before using the app. Do not deploy the committed placeholder rules as production configuration. The only page queries are single-field `createdAt desc` and event `at asc`; the Mac's envelope query is also single-field, so `firestore.indexes.json` needs no composite indexes.

6. **Set up App Check.** In Google Cloud Console's reCAPTCHA Enterprise section, create a **Website** key for your Hosting domain(s). Register the web app in **Firebase → App Check** using the **reCAPTCHA Enterprise** provider and that key. Put its site key in `config.local.json`. Deploy and open the app, check App Check metrics for **verified requests**, then turn on **Enforce for Firestore**. Callable enforcement is separately set in code with `enforceAppCheck`, true by default. The function's App Check option is captured at module load.

   Only while debugging App Check, set **`ENFORCE_APP_CHECK=false` in `functions/.env` and redeploy the function**. To keep the generated-file guard consistent, set `enforceAppCheck: false` in `config.local.json` and run `npm run render` (which writes that exact environment setting), then `firebase deploy --only functions --project <id>`. An edit to `.env` alone fails the freshness check. Restore `enforceAppCheck: true`, render, and redeploy promptly. Only the exact string `false` disables enforcement; unset or other values enforce it. This toggle does not disable Firestore App Check enforcement and never bypasses auth or the allowlist.

7. **Create service accounts and IAM permissions.** In **Google Cloud Console → IAM & Admin → Service Accounts**, create `approval-publisher@<project>.iam.gserviceaccount.com`. Grant it **only `roles/datastore.user` on the project**. Read [the contract's IAM section](CONTRACT.md#iam-and-custody) for custody and alternatives. The Admin SDK bypasses rules, and Firestore IAM cannot be scoped per collection. Store any publisher key outside all repositories, restrict it with `chmod 600`, never commit it, and rotate it. `iam.disableServiceAccountKeyCreation` may block keys; use an org-approved Workload Identity Federation, impersonation, or attached service-account setup instead.

   A separate function runtime service account with `roles/datastore.user` is recommended. Uncomment and fill the `serviceAccount` option in `functions/src/index.ts` to use it. Leaving it commented uses the default runtime identity; ensure that identity has Firestore data access.

8. **Render, check, install, and deploy.** From this directory, after filling all configuration:

   ```sh
   npm run render && npm run check
   cd functions && npm install && cd ..
   firebase deploy --only firestore:rules,firestore:indexes,functions,hosting --project <id>
   ```

   Replace `<id>` in commands with the real project ID (do not type literal angle brackets). Rendering generates `firestore.rules`, `public/config.js`, and `functions/.env`. The committed rules use `owner@example.invalid` solely to keep the template and repository self-consistent. **Deploy only after rendering real configuration.** The `--check` guard rejects missing files, `REPLACE_`, the placeholder email, unresolved template tokens, and outputs inconsistent with local config. Hosting and Functions predeploy hooks run it automatically; Functions also builds TypeScript. For a rules-only deployment, run `npm run check` explicitly first because Firestore has no hook here. Re-render after changing configuration or the rules template. Do not commit your real rendered allowlisted email when sharing the repository; review `firestore.rules` before committing.

   Second-generation callables may require `allUsers` to have `roles/run.invoker` on the underlying Cloud Run service so the callable endpoint is reachable. If invocation is blocked at Cloud Run, obtain the service name from the Functions/Cloud Run console and run, with all placeholders replaced:

   ```sh
   gcloud run services add-iam-policy-binding <UNDERLYING_CLOUD_RUN_SERVICE> --region us-central1 --project <PROJECT_ID> --member=allUsers --role=roles/run.invoker
   ```

   This makes the HTTP entry point reachable; the callable **still enforces Google auth, the verified-email allowlist, App Check, and rate limits**. An org policy may restrict public invocation; coordinate with your organization's administrator if so.

9. **Optional custom domain.** In **Hosting → Add custom domain**, follow DNS and certificate setup. Add the domain to Auth's authorized domains and the reCAPTCHA key's domains. Set `firebaseWebConfig.authDomain` to this custom domain and add `https://<that-domain>/__/auth/handler` to the Google OAuth client's **Authorized redirect URIs** as in step 2, then render/deploy again. Verify redirect sign-in on the owner's actual phone.

10. **Run local tests.** Install function dependencies with `cd functions && npm install && cd ..`, then from the root run:

    ```sh
    npm test
    ```

    This runs the dependency-free browser rendering tests, compiles strict TypeScript and runs function unit tests, then invokes `firebase emulators:exec --project demo-approval-web --only firestore` for rules and handler integration tests. Install **Java 21** for the emulator. The CLI may download emulator binaries on its first run. Emulator tests deliberately **fail**, rather than skip, when `FIRESTORE_EMULATOR_HOST` is missing. Test suites run serially against the demo project and clear their fixtures between tests; never point these tests at production. Tests do not need real config or `npm run render` because they use the template with `owner@example.test`. The handler integration tests call the inner handler and do not test the App Check HTTP transport.

    Individual commands: `npm run test:render`, `npm run test:unit`, `npm run test:emu`. The one-line `tests/index.js` entry point lets Node 22 resolve the required `node --test tests/` directory command. There are no root dependencies and firebase-tools is intentionally not a dependency.

    For optional manual browser testing, set `useEmulators: true` in `config.local.json` and render. Only `localhost`/`127.0.0.1` connect Auth (9099), Firestore (8080), and Functions (5001) emulators. Set optional `appCheckDebug: true` **only for localhost testing**, register the generated debug token in Firebase App Check, and keep the token private. On `127.0.0.1` the debug-token shortcut intentionally does not activate. Start emulators explicitly for the demo project and serve `public/` locally (for example with Hosting emulator added on the CLI). Emulator UI is disabled. Hosting's production CSP deliberately does not permit local emulator connections; if testing through Hosting, use a separate, local-only copy of the Hosting configuration with localhost connect/frame sources, and never deploy that relaxed CSP. Remove local flags and render before deploying. Automated tests do not need this browser setup.

11. **Connect the Mac and verify end to end.** Follow [CONTRACT.md](CONTRACT.md) to publish requests, consume envelopes using local versions, acknowledge them, and mirror every Telegram/system transition. Add `https://<hosting-domain>/r/<requestId>` to each Telegram request message. Use a test request for this checklist:

    - Sign in with the wrong Google account: the log/request view says **“This Google account is not allowed”** and offers Sign out.
    - Open a pending request on the phone and press Yes: confirm the action, see the waiting-for-Mac message, then see the Mac's applied status and history. Verify the usual Telegram confirmation arrives marked **“via web”**.
    - Try the other answer while the first is in flight: controls are disabled, and direct repeat calls are rejected.
    - Use two tabs or temporarily disconnect one tab; advance the version through Telegram, then submit the old tab before it receives the update. The stale version must be rejected. A live connected tab should update immediately and may no longer be stale.
    - Try pending and declined requests at/after expiry: the page offers no action; a direct stale page submission is rejected as expired. An approved request must still offer **Change to No** after expiry.
    - Change No to Yes on a declined request and confirm the Mac reruns its precheck. Change an approved request to No during grace and after launch; verify the Mac's usual decline/cancel behavior.
    - Verify live updates for expiry sweep and finished, link withholding for a mismatched PR/issue URL, and SHA copy. Refresh `/r/<id>` and `/log` directly to verify Hosting rewrites.

## Security model

Only the one configured Google account, with verified email and the Google sign-in provider, can read requests or events. **There are no client writes at all**, including to events, envelopes, and rate limits. The function checks auth before accessing Firestore, validates exact input shape, limits calls by UID, and atomically queues one answer per current version. It hashes the email for events and logs only request ID, decision, and outcome code, never email or request payload. Browser text is rendered as text; GitHub links are constrained to this repository and the displayed PR/issue number.

App Check helps reject calls from unrecognized app clients. It is not user authentication, a substitute for the allowlist, or a guarantee against automation or abuse by a legitimate signed-in user. The callable applies it separately from Firestore's console enforcement. The Mac uses Admin credentials and bypasses these rules; IAM and key custody protect that side. Event immutability and field ownership must also be respected by the trusted Mac code because Admin IAM can bypass client rules.

If the allowlisted Google account is compromised, an attacker could approve a tester build. The Mac's **2-minute grace**, Telegram confirmation, change-your-answer behavior, and its own prechecks remain mitigations, not a guarantee of prevention. Protect that Google account with **2-step verification and/or a passkey**. This page never grants access to other data and never directly starts a build.
