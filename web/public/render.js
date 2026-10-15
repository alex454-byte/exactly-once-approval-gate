const requestIdPattern = /^[A-Za-z0-9_-]{22}$/;
const githubPattern = /^https:\/\/github\.com\/example-org\/Example-App\/(pull|issues)\/([1-9][0-9]{0,6})$/;

export function validateGithubUrl(url, expectedKind, expectedNumber) {
  if (typeof url !== 'string') return false;
  const match = githubPattern.exec(url);
  if (!match || match[0] !== url) return false;
  if (expectedKind !== undefined && expectedKind !== match[1]) return false;
  if (expectedNumber !== undefined && expectedNumber !== Number(match[2])) return false;
  try {
    const parsed = new URL(url);
    return parsed.protocol === 'https:' && parsed.hostname === 'github.com'
      && parsed.port === '' && parsed.username === '' && parsed.password === ''
      && parsed.search === '' && parsed.hash === ''
      && parsed.pathname === `/example-org/Example-App/${match[1]}/${match[2]}`;
  } catch {
    return false;
  }
}

export function timestampMillis(value) {
  try {
    const result = value instanceof Date ? value.getTime()
      : typeof value === 'number' ? value
        : typeof value?.toMillis === 'function' ? value.toMillis() : NaN;
    return Number.isFinite(result) ? result : null;
  } catch {
    return null;
  }
}

export function allowedActions(status, expiresAt, now = Date.now()) {
  if (status === 'approved') return ['no'];
  if (status !== 'pending' && status !== 'declined') return [];
  const expiry = timestampMillis(expiresAt);
  const time = timestampMillis(now);
  if (expiry === null || time === null || time >= expiry) return [];
  return status === 'pending' ? ['yes', 'no'] : ['yes'];
}

export function statusLabel(status) {
  const labels = {
    pending: 'Pending', approved: 'Approved', declined: 'Declined',
    cancel_requested: 'Cancel requested', expired: 'Expired', finished: 'Finished',
  };
  return Object.hasOwn(labels, status) ? labels[status] : 'Unknown';
}

export function parseRoute(pathname) {
  if (pathname === '/') return { name: 'home' };
  if (pathname === '/log') return { name: 'log' };
  if (typeof pathname !== 'string' || !pathname.startsWith('/r/')) return { name: 'notfound' };
  const id = pathname.slice(3);
  return id.length === 22 && requestIdPattern.test(id) ? { name: 'request', id } : { name: 'notfound' };
}

const string = (value, fallback = '') => typeof value === 'string' ? value : fallback;
const integer = (value) => Number.isSafeInteger(value) ? value : null;

export function formatRequestModel(value) {
  const doc = value && typeof value === 'object' ? value : {};
  const test = doc.test === true || doc.kind === 'test-echo';
  const buildNumberExpected = integer(doc.buildNumberExpected);
  const lastUploadedBuild = integer(doc.lastUploadedBuild);
  const localBuildMax = integer(doc.localBuildMax);
  const status = string(doc.status, 'unknown');
  const version = integer(doc.version);
  const pending = doc.pendingWebDecision;
  return {
    title: `${test ? '[TEST] ' : ''}Tester upload +${buildNumberExpected ?? '?'}`,
    test, buildNumberExpected, lastUploadedBuild, localBuildMax,
    hasLocalBuilds: localBuildMax !== null && lastUploadedBuild !== null && localBuildMax > lastUploadedBuild,
    sha: string(doc.sha), shortSha: string(doc.sha).slice(0, 8), appCommits: integer(doc.appCommits),
    status, statusText: statusLabel(status), version,
    createdAt: timestampMillis(doc.createdAt), expiresAt: timestampMillis(doc.expiresAt),
    updatedAt: timestampMillis(doc.updatedAt), decidedAt: timestampMillis(doc.decidedAt),
    decidedReason: string(doc.decidedReason),
    prs: (Array.isArray(doc.prs) ? doc.prs : []).filter((pr) => pr && typeof pr === 'object').map((pr) => ({
      number: integer(pr.number), title: string(pr.title), url: string(pr.url),
      issues: (Array.isArray(pr.issues) ? pr.issues : []).filter((issue) => issue && typeof issue === 'object').map((issue) => ({
        number: integer(issue.number), title: string(issue.title), url: string(issue.url),
      })),
    })),
    history: (Array.isArray(doc.history) ? doc.history : []).filter((item) => item && typeof item === 'object').map((item) => ({
      version: integer(item.version), status: string(item.status), at: timestampMillis(item.at),
      reason: string(item.reason), via: string(item.via, 'system'),
    })),
    pendingWebDecision: pending && (pending.decision === 'yes' || pending.decision === 'no')
      && Number.isSafeInteger(pending.expectedVersion) ? {
        decision: pending.decision, expectedVersion: pending.expectedVersion,
        eventId: string(pending.eventId), at: timestampMillis(pending.at),
      } : null,
  };
}

const attributes = new Set(['class', 'id', 'href', 'type', 'disabled', 'role', 'aria-live', 'aria-label', 'rel', 'target']);
export function el(document, tag, text, attrs = {}) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  for (const [name, value] of Object.entries(attrs)) {
    if (!attributes.has(name)) throw new Error(`Unsupported attribute: ${name}`);
    node.setAttribute(name, String(value));
  }
  return node;
}
