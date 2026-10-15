import { readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';

const root = new URL('../', import.meta.url);
const placeholderEmail = 'owner@example.invalid';
const fail = (message) => { throw new Error(message); };
const plainObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);

async function read(relative) {
  try {
    return await readFile(new URL(relative, root), 'utf8');
  } catch {
    fail(`Missing or unreadable ${relative}. Copy config.example.json to config.local.json, fill it in, then run npm run render.`);
  }
}

function validate(config) {
  if (!plainObject(config)) fail('config.local.json must contain a JSON object.');
  if (JSON.stringify(config).includes('REPLACE_')) fail('Replace every REPLACE_ placeholder in config.local.json.');
  if (typeof config.projectId !== 'string' || /\s/.test(config.projectId) || !/^[a-z][a-z0-9-]{4,28}[a-z0-9]$/.test(config.projectId)) {
    fail('projectId must be 6–30 lowercase letters, digits or hyphens, beginning with a letter and ending with a letter or digit.');
  }
  if (typeof config.allowedEmail !== 'string' || /['"\\\r\n]/.test(config.allowedEmail)
      || !/^[A-Za-z0-9.!#$%&*+\/?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}$/.test(config.allowedEmail)) {
    fail('allowedEmail must be an email address without quotes, backslashes or whitespace.');
  }
  const email = config.allowedEmail.toLowerCase();
  if (email === placeholderEmail) fail('The committed placeholder email cannot be deployed.');
  if (!plainObject(config.firebaseWebConfig)) fail('firebaseWebConfig must be an object.');
  for (const key of ['apiKey', 'authDomain', 'projectId', 'storageBucket', 'messagingSenderId', 'appId']) {
    if (typeof config.firebaseWebConfig[key] !== 'string' || !config.firebaseWebConfig[key].trim()) {
      fail(`firebaseWebConfig.${key} must be a non-empty string.`);
    }
  }
  if (config.firebaseWebConfig.projectId !== config.projectId) fail('Both projectId values must match.');
  if (typeof config.appCheckSiteKey !== 'string' || !config.appCheckSiteKey.trim()) fail('appCheckSiteKey is required.');
  if (typeof config.enforceAppCheck !== 'boolean') fail('enforceAppCheck must be true or false.');
  for (const key of ['useEmulators', 'appCheckDebug']) {
    if (config[key] !== undefined && typeof config[key] !== 'boolean') fail(`${key} must be a boolean when present.`);
  }
  return email;
}

async function main() {
  if (process.argv.slice(2).some((arg) => arg !== '--check')) fail('Usage: node scripts/render-config.mjs [--check]');
  const config = JSON.parse(await read('config.local.json'));
  const email = validate(config);
  const template = await read('firestore.rules.template');
  if (!template.includes('__ALLOWED_EMAIL__')) fail('The rules template is missing __ALLOWED_EMAIL__.');
  // Quotes and backslashes are rejected above; the remaining email is safe in a rules string literal.
  const webConfig = { ...config.firebaseWebConfig, appCheckSiteKey: config.appCheckSiteKey };
  for (const key of ['useEmulators', 'appCheckDebug']) {
    if (config[key] !== undefined) webConfig[key] = config[key];
  }
  const outputs = new Map([
    ['firestore.rules', template.replaceAll('__ALLOWED_EMAIL__', () => email)],
    ['public/config.js', `window.APPROVAL_CONFIG = ${JSON.stringify(webConfig, null, 2)};\n`],
    ['functions/.env', `ALLOWED_EMAIL="${email}"\nENFORCE_APP_CHECK=${config.enforceAppCheck}\n`],
  ]);
  if (process.argv.includes('--check')) {
    for (const [path, expected] of outputs) {
      const actual = await read(path);
      if (actual.includes('REPLACE_') || actual.includes(placeholderEmail) || actual.includes('__ALLOWED_EMAIL__')) {
        fail(`${path} still contains a placeholder. Run npm run render.`);
      }
      if (actual !== expected) fail(`${path} is stale or was edited manually. Update config.local.json and run npm run render.`);
    }
    console.log('Generated configuration is ready.');
    return;
  }
  for (const [path, content] of outputs) await writeFile(new URL(path, root), content, { mode: 0o600 });
  console.log(`Rendered firestore.rules, public/config.js and functions/.env in ${fileURLToPath(root)}`);
}

main().catch((error) => {
  console.error(`Configuration error: ${error.message}`);
  process.exitCode = 1;
});
