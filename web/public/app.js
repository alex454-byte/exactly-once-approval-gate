import { initializeApp } from 'https://www.gstatic.com/firebasejs/11.10.0/firebase-app.js';
import {
  getAuth, GoogleAuthProvider, signInWithPopup, signInWithRedirect, getRedirectResult,
  onAuthStateChanged, signOut, connectAuthEmulator,
} from 'https://www.gstatic.com/firebasejs/11.10.0/firebase-auth.js';
import {
  getFirestore, doc, collection, query, orderBy, limit, onSnapshot, connectFirestoreEmulator,
} from 'https://www.gstatic.com/firebasejs/11.10.0/firebase-firestore.js';
import { getFunctions, httpsCallable, connectFunctionsEmulator } from 'https://www.gstatic.com/firebasejs/11.10.0/firebase-functions.js';
import { initializeAppCheck, ReCaptchaEnterpriseProvider } from 'https://www.gstatic.com/firebasejs/11.10.0/firebase-app-check.js';
import { el, validateGithubUrl, formatRequestModel, allowedActions, statusLabel, parseRoute, timestampMillis } from './render.js';

const content = document.getElementById('content');
const authPanel = document.getElementById('auth');
const message = document.getElementById('message');
const route = parseRoute(window.location.pathname);
const make = (tag, text, attrs) => el(document, tag, text, attrs);
const localTime = (time) => time === null ? 'Unknown' : new Date(time).toLocaleString();
const say = (text) => { message.textContent = text; };

function errorMessage(error) {
  const reason = error?.details?.reason;
  const reasons = {
    stale: 'This request changed since you opened it. The page has updated — check it and answer again.',
    expired: 'This request has expired and can no longer be answered.',
    'not-allowed': 'This request has already been handled. Check its current status.',
    'in-flight': 'Your answer was already sent — waiting for the Mac to apply it.',
  };
  if (Object.hasOwn(reasons, reason)) return reasons[reason];
  const code = typeof error?.code === 'string' ? error.code.split('/').pop() : '';
  const messages = {
    'resource-exhausted': 'Too many attempts. Please wait and try again later.',
    'permission-denied': 'This Google account is not allowed',
    unauthenticated: 'Please sign in with Google again. If you are already signed in, sign out and retry.',
    'not-found': 'This request was not found.',
    'popup-closed-by-user': 'Sign-in was cancelled. Try again when you are ready.',
  };
  return Object.hasOwn(messages, code) ? messages[code] : 'Something went wrong. Please try again.';
}

function githubLink(item, kind) {
  const text = `#${item.number ?? '?'}${item.title ? ` ${item.title}` : ''}`;
  if (!validateGithubUrl(item.url, kind, item.number)) return make('span', `${text} (link withheld)`);
  return make('a', text, { href: item.url, rel: 'noopener noreferrer', target: '_blank' });
}

function historyLines(model, events) {
  const lines = model.history.map((entry) => ({
    at: entry.at, via: entry.via,
    text: `${statusLabel(entry.status)}${entry.reason ? ` — ${entry.reason}` : ''}`,
  }));
  for (const event of events) {
    const at = timestampMillis(event.at);
    const reason = typeof event.reason === 'string' ? event.reason : '';
    if (event.type === 'web_decision') {
      lines.push({ at, text: `Answer ${event.decision === 'yes' ? 'YES' : 'NO'} queued`, via: 'web' });
    } else if (event.type === 'web_decision_applied' || event.type === 'web_decision_rejected') {
      // The history entry records an applied transition; the event additionally records acknowledgement.
      lines.push({ at, text: `Web answer ${event.type === 'web_decision_applied' ? 'applied' : 'rejected'}${reason ? ` — ${reason}` : ''}`, via: 'web' });
    } else if (event.type === 'telegram_decision' || event.type === 'system') {
      const via = event.type === 'telegram_decision' ? 'telegram' : 'system';
      const mirrored = model.history.some((entry) => entry.version === event.version && entry.status === event.status
        && entry.at === at && entry.reason === reason && entry.via === via);
      if (!mirrored) lines.push({ at, text: `${statusLabel(event.status)}${reason ? ` — ${reason}` : ''}`, via });
    }
  }
  return lines.sort((a, b) => (a.at ?? 0) - (b.at ?? 0));
}

function start() {
  const config = window.APPROVAL_CONFIG;
  if (!config?.projectId || !config?.appCheckSiteKey) {
    content.replaceChildren(make('p', 'This approval page has not been configured.'));
    return;
  }
  const app = initializeApp(config);
  if (config.appCheckDebug === true && window.location.hostname === 'localhost') {
    self.FIREBASE_APPCHECK_DEBUG_TOKEN = true;
  }
  initializeAppCheck(app, {
    provider: new ReCaptchaEnterpriseProvider(config.appCheckSiteKey), isTokenAutoRefreshEnabled: true,
  });
  const auth = getAuth(app);
  const db = getFirestore(app);
  const functions = getFunctions(app, 'us-central1');
  if (['localhost', '127.0.0.1'].includes(window.location.hostname) && config.useEmulators === true) {
    connectAuthEmulator(auth, 'http://127.0.0.1:9099', { disableWarnings: true });
    connectFirestoreEmulator(db, '127.0.0.1', 8080);
    connectFunctionsEmulator(functions, '127.0.0.1', 5001);
  }
  const submitDecision = httpsCallable(functions, 'submitDecision');
  let unsubscribers = [];
  let refreshTimer;
  let generation = 0;

  function stopListening() {
    generation += 1;
    for (const unsubscribe of unsubscribers) unsubscribe();
    unsubscribers = [];
    window.clearInterval(refreshTimer);
  }

  function readError(error) {
    stopListening();
    say(errorMessage(error));
    content.replaceChildren();
  }

  function renderAuth(user) {
    authPanel.replaceChildren();
    if (user) {
      authPanel.append(make('span', user.email ?? 'Signed in', { class: 'account' }));
      const button = make('button', 'Sign out', { type: 'button' });
      button.addEventListener('click', async () => {
        button.disabled = true;
        try {
          await signOut(auth);
        } catch (error) {
          say(errorMessage(error));
          button.disabled = false;
        }
      });
      authPanel.append(button);
      return;
    }
    const button = make('button', 'Sign in with Google', { type: 'button' });
    button.addEventListener('click', async () => {
      button.disabled = true;
      say('');
      try {
        const provider = new GoogleAuthProvider();
        provider.setCustomParameters({ prompt: 'select_account' });
        const mobile = navigator.maxTouchPoints > 0 || window.matchMedia('(pointer: coarse)').matches;
        if (mobile) await signInWithRedirect(auth, provider);
        else {
          try { await signInWithPopup(auth, provider); }
          catch (error) {
            if (error.code === 'auth/popup-blocked') await signInWithRedirect(auth, provider);
            else throw error;
          }
        }
      } catch (error) {
        say(errorMessage(error));
      } finally {
        button.disabled = false;
      }
    });
    authPanel.append(button);
  }

  function showRequest(id) {
    let model = null;
    let events = [];
    let sendingDecision = null;
    const currentGeneration = generation;
    const ref = doc(db, 'approval_requests', id);

    async function answer(decision) {
      if (!model || sendingDecision) return;
      const expectedVersion = model.version;
      if (!window.confirm(`${decision === 'yes' ? 'Send YES — upload' : 'Send NO — skip'} for ${model.title}?`)) return;
      sendingDecision = decision;
      say('');
      render();
      try {
        await submitDecision({ requestId: id, decision, expectedVersion });
      } catch (error) {
        if (currentGeneration === generation) say(errorMessage(error));
      } finally {
        sendingDecision = null;
        if (currentGeneration === generation) render();
      }
    }

    function render() {
      if (!model) return;
      const now = Date.now();
      const card = make('article', null, { class: 'card' });
      card.append(make('h1', model.title), make('p', model.statusText, { class: 'badge' }));
      const facts = make('dl');
      const fact = (label, value) => facts.append(make('dt', label), make('dd', value));
      fact('Last uploaded', `+${model.lastUploadedBuild ?? '?'}`);
      fact('App commits', String(model.appCommits ?? '?'));
      fact('Created', localTime(model.createdAt));
      card.append(facts);
      if (model.hasLocalBuilds) card.append(make('p', `Local builds reach +${model.localBuildMax}, ahead of the last uploaded build.`, { class: 'note' }));
      const sha = make('div', null, { class: 'sha-row' });
      sha.append(make('span', model.sha || 'SHA unavailable', { class: 'sha' }));
      const copy = make('button', 'Copy SHA', { type: 'button', 'aria-label': 'Copy full commit SHA' });
      copy.disabled = !model.sha;
      copy.addEventListener('click', async () => {
        try {
          await navigator.clipboard.writeText(model.sha);
          say('SHA copied.');
        } catch { say('Copy is unavailable. Select the SHA to copy it.'); }
      });
      sha.append(copy);
      card.append(sha);
      if (model.expiresAt === null) card.append(make('p', 'Expiry unavailable.'));
      else {
        const minutes = Math.ceil(Math.max(0, model.expiresAt - now) / 60_000);
        const remaining = now >= model.expiresAt ? 'expired' : `expires in ${Math.floor(minutes / 60)}h ${minutes % 60}m`;
        card.append(make('p', `${localTime(model.expiresAt)} — ${remaining}`, { class: 'muted' }));
        if (model.status === 'approved' && now >= model.expiresAt) card.append(make('p', 'You can still change an approved request to No.'));
      }
      if (model.decidedAt !== null || model.decidedReason) {
        card.append(make('p', `Decided ${localTime(model.decidedAt)}${model.decidedReason ? ` — ${model.decidedReason}` : ''}`));
      }
      card.append(make('h2', 'Pull requests'));
      const prs = make('ul', null, { class: 'pr-list' });
      for (const pr of model.prs) {
        const row = make('li');
        row.append(githubLink(pr, 'pull'));
        if (pr.issues.length) {
          const issues = make('ul');
          for (const issue of pr.issues) {
            const item = make('li');
            item.append(githubLink(issue, 'issues'));
            issues.append(item);
          }
          row.append(issues);
        }
        prs.append(row);
      }
      card.append(model.prs.length ? prs : make('p', 'No pull requests listed.', { class: 'muted' }));
      const pending = model.pendingWebDecision?.expectedVersion === model.version ? model.pendingWebDecision : null;
      if (pending) card.append(make('p', `Your answer (${pending.decision.toUpperCase()}) was sent — waiting for the Mac to apply it`, { class: 'notice', role: 'status' }));
      const actions = make('div', null, { class: 'actions' });
      for (const decision of allowedActions(model.status, model.expiresAt, now)) {
        const label = model.status === 'pending'
          ? decision === 'yes' ? 'Yes — upload' : 'No — skip'
          : decision === 'yes' ? 'Change to Yes' : 'Change to No';
        const button = make('button', sendingDecision === decision ? 'Sending…' : label, { type: 'button', class: decision });
        button.disabled = Boolean(sendingDecision) || Boolean(pending) || model.version === null;
        button.addEventListener('click', () => answer(decision));
        actions.append(button);
      }
      card.append(actions, make('h2', 'Decision history'));
      const history = make('ol', null, { class: 'history' });
      for (const line of historyLines(model, events)) history.append(make('li', `${localTime(line.at)} — ${line.text} — via ${line.via}`));
      card.append(history.childElementCount ? history : make('p', 'No history yet.', { class: 'muted' }));
      content.replaceChildren(card);
    }

    unsubscribers.push(onSnapshot(ref, (snapshot) => {
      if (!snapshot.exists()) {
        model = null;
        content.replaceChildren(make('h1', 'Request not found'));
        return;
      }
      model = formatRequestModel(snapshot.data());
      render();
    }, readError));
    unsubscribers.push(onSnapshot(query(collection(ref, 'events'), orderBy('at', 'asc')), (snapshot) => {
      events = snapshot.docs.map((item) => item.data());
      render();
    }, readError));
    refreshTimer = window.setInterval(render, 30_000);
  }

  function showLog() {
    const recent = query(collection(db, 'approval_requests'), orderBy('createdAt', 'desc'), limit(50));
    unsubscribers.push(onSnapshot(recent, (snapshot) => {
      const list = make('ol', null, { class: 'request-log' });
      for (const item of snapshot.docs) {
        const model = formatRequestModel(item.data());
        const row = make('li', null, { class: 'card' });
        const path = `/r/${item.id}`;
        row.append(parseRoute(path).name === 'request'
          ? make('a', model.title, { href: path }) : make('span', model.title));
        row.append(make('p', `${model.shortSha || 'No SHA'} · ${model.statusText}`));
        row.append(make('p', localTime(model.createdAt), { class: 'muted' }));
        row.append(make('p', model.decidedReason || 'No decision reason yet.'));
        list.append(row);
      }
      content.replaceChildren(make('h1', 'Latest requests'), make('p', 'The 50 most recent requests.', { class: 'muted' }),
        snapshot.empty ? make('p', 'No requests yet.') : list);
    }, readError));
  }

  onAuthStateChanged(auth, (user) => {
    stopListening();
    say('');
    renderAuth(user);
    if (route.name === 'notfound') content.replaceChildren(make('h1', 'Not found'));
    else if (route.name === 'home') content.replaceChildren(make('h1', 'Tester build approvals'),
      make('p', 'Open a request from Telegram to answer it, or view the recent request log.'), make('a', 'View request log', { href: '/log' }));
    else if (!user) content.replaceChildren(make('p', 'Sign in with Google to view this page.'));
    else {
      content.replaceChildren(make('p', 'Loading…'));
      if (route.name === 'request') showRequest(route.id);
      else showLog();
    }
  }, (error) => say(errorMessage(error)));
  getRedirectResult(auth).catch((error) => say(errorMessage(error)));
  window.addEventListener('pagehide', stopListening);
}

try { start(); }
catch { content.replaceChildren(make('p', 'The approval page could not start. Please reload and try again.')); }
