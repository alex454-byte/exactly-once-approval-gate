"""Local, exactly-once Telegram approval gate. Standard library only.

An agent (or a script) asks a human a yes/no question over Telegram. The human's
answer is recorded exactly once, under a file lock, as a numbered version claim.
Only an approved request ever launches its runner, after a grace period during
which the human can still change their mind.
"""
import argparse
import contextlib
import dataclasses
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

import ledger

UTC = dt.timezone.utc
ID_RE = re.compile(r"[A-Za-z0-9_-]{22}\Z")
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
OWNER_REASONS = ("owner", "changed to YES", "changed to NO")
# Secrets are registered before any transport operation; audit is a final scrub boundary.
_SECRETS = set()


def scrub(value):
    text = str(value)
    for secret in sorted(_SECRETS, key=len, reverse=True):
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def utcnow():
    return dt.datetime.now(UTC)


def iso(now):
    return now.astimezone(UTC).isoformat()


def stamp(text):
    return dt.datetime.fromisoformat(text)


def local_tz():
    return ZoneInfo(os.environ.get("APPROVALS_TZ", "UTC"))


@dataclasses.dataclass(frozen=True)
class Paths:
    state: Path

    @property
    def requests(self):
        return self.state / "requests"


def paths(create=True):
    p = Paths(Path(os.environ.get("APPROVALS_STATE_DIR", "~/.agent-state/approvals")).expanduser())
    if create:
        private_dir(p.state)
        for name in ("requests", "alerts", "runs"):
            private_dir(p.state / name)
    return p


def private_dir(path):
    Path(path).mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def atomic_write_json(path, obj):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    # Failed writes deliberately retain their temporary file for inspection.
    with os.fdopen(fd, "w") as out:
        os.fchmod(out.fileno(), 0o600)
        json.dump(obj, out, ensure_ascii=False)
        out.write("\n")
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, path)
    dfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def load_config(read_only=False):
    path = paths(create=not read_only).state / "config.json"
    try:
        with path.open() as source:
            config = json.load(source)
    except FileNotFoundError:
        raise RuntimeError("Missing approvals config.json: create it with integer owner_user_id and chat_id before running") from None
    except (ValueError, OSError):
        raise RuntimeError("Cannot read approvals config.json") from None
    if not isinstance(config, dict) or any(type(config.get(k)) is not int for k in ("owner_user_id", "chat_id")):
        raise RuntimeError("Invalid approvals config.json: owner_user_id and chat_id must be integers")
    _SECRETS.add(str(config["chat_id"]))
    if not read_only:
        os.chmod(path, 0o600)
    return config


def audit(event, **fields):
    line = scrub(json.dumps({"ts": iso(utcnow()), "event": event, **fields}, ensure_ascii=False))
    fd = os.open(paths().state / "audit.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a") as out:
        fcntl.flock(out, fcntl.LOCK_EX)
        os.fchmod(out.fileno(), 0o600)
        out.write(line + "\n")
        out.flush()
        os.fsync(out.fileno())


def hash_id(from_id):
    return hashlib.sha256(str(from_id).encode()).hexdigest()[:16]


def validate_id(req_id):
    if not isinstance(req_id, str) or not ID_RE.fullmatch(req_id):
        raise ValueError("Invalid request id")
    return req_id


@contextlib.contextmanager
def store_lock():
    fd = os.open(paths().requests / ".store.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _load_request(req_id):
    p = paths().requests
    with (p / (req_id + ".json")).open() as source:
        record = json.load(source)
    claim = p / (req_id + ".claim")
    if record["status"] == "pending" and claim.exists():
        try:
            decision = json.loads(claim.read_text())
            if decision["status"] not in ("approved", "skipped", "expired"):
                raise ValueError()
            record.update({k: decision[k] for k in ("status", "decided_at", "decided_reason")})
        except (ValueError, KeyError):
            # Never reopen an acquired gate, even if a crash interrupted its write.
            raise RuntimeError("Incomplete decision claim; manual recovery required") from None
        atomic_write_json(p / (req_id + ".json"), record)
    if record["status"] == "skipped":
        record["status"] = "declined"
    record.setdefault("version", 0)
    record.setdefault("history", [])
    record.setdefault("live_message_id", record.get("message_id"))
    record.setdefault("launch_at", None)
    record.setdefault("launched", record["status"] == "approved")
    claims = sorted(p.glob(req_id + ".v*.claim"), key=lambda f: int(f.name.split(".v")[1].split(".")[0]))
    for claim in claims:
        version = int(claim.name.split(".v")[1].split(".")[0])
        if version <= record["version"]:
            continue
        try:
            decision = json.loads(claim.read_text())
            if version != record["version"] + 1 or decision["version"] != version:
                raise ValueError()
            record.update(decision)
        except (ValueError, KeyError):
            raise RuntimeError("Incomplete versioned claim; manual recovery required") from None
        atomic_write_json(p / (req_id + ".json"), record)
    if record["status"] == "cancel_requested":
        durable_line(paths().state / "runs" / (req_id + ".cancel"), "cancel requested", once=True)
    persist_events(record)
    return record


def load_request(req_id):
    validate_id(req_id)  # Must precede paths(), not just the filename join.
    with store_lock():
        return _load_request(req_id)


def create_request(kind, payload, now, reask_of=None):
    if kind not in KINDS:
        raise ValueError("Unknown request kind")
    req_id = secrets.token_urlsafe(16)
    while not req_id[0].isalnum():
        req_id = secrets.token_urlsafe(16)
    record = dict(id=req_id, kind=kind, payload=payload,
                  status="pending", created_at=iso(now), expires_at=iso(now + dt.timedelta(hours=12)),
                  message_id=None, decided_at=None, decided_reason=None, version=0, history=[],
                  live_message_id=None, launch_at=None, launched=False)
    with store_lock():
        previous = [json.loads(p.read_text()) for p in paths().requests.glob('*.json')
                    if ID_RE.fullmatch(p.stem)]
        previous = [r for r in previous if r['kind'] == kind]
        record['risk_repeat'] = bool(previous)
        record['reask_of'] = reask_of
        record['risk'] = KINDS[kind].risk(record, {'previous': previous})
        atomic_write_json(paths().requests / (record["id"] + ".json"), record)
    audit("request_created", id=record["id"], kind=kind)
    return record


def set_message_id(req_id, message_id, sent_at=None, ctx=None):
    validate_id(req_id)
    with store_lock():
        record = _load_request(req_id)
        record["message_id"] = message_id
        if sent_at is not None:
            record["sent_at"] = iso(sent_at)
        if record["version"] == 0:
            record["live_message_id"] = message_id
        atomic_write_json(paths().requests / (req_id + ".json"), record)
        if sent_at is not None:
            requested_ledger(record, ctx)
    return record


def all_requests():
    return [load_request(p.stem) for p in sorted(paths().requests.glob("*.json")) if ID_RE.fullmatch(p.stem)]


def durable_line(path, line, once=False, key=None):
    private_dir(path.parent)
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a+") as out:
        fcntl.flock(out, fcntl.LOCK_EX)
        os.fchmod(out.fileno(), 0o600)
        if key:
            marker = "<!-- " + key + " -->"
            out.seek(0)
            if any(marker in existing for existing in out):
                return
            line += " " + marker
        if not once or os.fstat(out.fileno()).st_size == 0:
            out.write(scrub(line) + "\n")
            out.flush()
            os.fsync(out.fileno())


def is_test(record):
    return record["kind"] == "test-echo" or record["payload"].get("test", False)


def grace_seconds():
    return max(0, int(os.environ.get("APPROVALS_GRACE_SECONDS", "120")))


def cancel_reason(req_id):
    started = (paths().state / "runs" / (req_id + ".started")).exists()
    return "cancel requested — too late, run had started" if started else "cancel requested before run"


def transition(req_id, version, status, reason, now, ctx=None, **changes):
    validate_id(req_id)
    with store_lock():
        record = _load_request(req_id)
        if record["version"] != version:
            return False, record
        if status == "cancel_requested":
            reason = cancel_reason(req_id)
        event = dict(version=version + 1, status=status, at=iso(now), reason=reason)
        decision = dict(version=version + 1, status=status, decided_at=iso(now), decided_reason=reason,
                        history=record["history"] + [event], **changes)
        path = paths().requests / (req_id + f".v{version + 1}.claim")
        try:
            # O_EXCL is the exactly-once point: only one writer can create version N+1.
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False, _load_request(req_id)
        with os.fdopen(fd, "w") as out:
            json.dump(decision, out)
            out.flush()
            os.fsync(out.fileno())
        record.update(decision)
        atomic_write_json(paths().requests / (req_id + ".json"), record)
        if status == "cancel_requested":
            durable_line(paths().state / "runs" / (req_id + ".cancel"), "cancel requested", once=True)
        persist_events(record, ctx)
    return True, record


def persist_events(record, ctx=None):
    """Replay local event delivery after a claim/JSON crash, under store_lock."""
    pending = [e for e in record['history'] if e['version'] > record.get('logged_version', 0)]
    if not pending:
        return
    req_id = record['id']
    for event in pending:
        status, reason = event['status'], event['reason']
        decision_ledger(record, event, ctx)
        audit('request_decided', id=req_id, status=status, reason=reason, version=event['version'])
        payload = record['payload']
        local = stamp(event['at']).astimezone(local_tz())
        choice = 'YES' if status == 'approved' and reason in ('owner', 'changed to YES') else 'NO' if status == 'declined' else status
        source = 'via Telegram' if reason in OWNER_REASONS else 'automatic'
        line = (f"- {local:%Y-%m-%d %H:%M %Z} | {'[TEST] ' if is_test(record) else ''}{record['kind']} | "
                f"+{payload.get('build_number_expected', '—')} | {payload.get('sha', '—')[:8]} | {choice} | {source} | {reason}")
        if choice == 'YES' and record.get('launch_at'):
            seconds = (stamp(record['launch_at']) - stamp(event['at'])).total_seconds()
            line += f"; grace {seconds / 60:g}m"
        log = os.environ.get('APPROVALS_DECISIONS_LOG')
        durable_line(Path(log).expanduser() if log else paths().state / 'decisions.md',
                     line, key=f"request {req_id} v{event['version']}")
    record['logged_version'] = record['version']
    atomic_write_json(paths().requests / (req_id + '.json'), record)


def decide(req_id, new_status, reason, now, ctx=None):
    if new_status not in ("approved", "skipped", "declined", "expired"):
        raise ValueError("Invalid decision")
    record = load_request(req_id)
    if record["status"] != "pending":
        return False, record
    status = "declined" if new_status == "skipped" else new_status
    changes = {"launch_at": iso(now + dt.timedelta(seconds=grace_seconds()))} if status == "approved" else {}
    return transition(req_id, record["version"], status, reason, now, ctx=ctx, **changes)


def callback_data(req_id, action, version=0):
    validate_id(req_id)
    if action not in ("a", "s", "y", "n") or type(version) is not int or version < 0:
        raise ValueError("Invalid callback action/version")
    data = f"ap2:{req_id}:{action}:{version}"
    if len(data.encode()) > 64:
        raise ValueError("Callback exceeds Telegram limit")
    return data


def parse_callback(data):
    if not isinstance(data, str) or len(data.encode()) > 64:
        raise ValueError("Invalid callback")
    legacy = re.fullmatch(r"ap1:([A-Za-z0-9_-]{22}):([as])", data)
    if legacy:
        return (*legacy.groups(), 0)
    match = re.fullmatch(r"ap2:([A-Za-z0-9_-]{22}):([asyn]):(0|[1-9][0-9]*)", data)
    if not match:
        raise ValueError("Invalid callback")
    return match[1], match[2], int(match[3])


def git(*args):
    try:
        result = subprocess.run(["git", *map(str, args)], capture_output=True, text=True, timeout=120, check=True)
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError("git command failed (timeout, unavailable command, or nonzero exit)") from None


def main_sha(git_call=git):
    repo = os.environ.get("APPROVALS_REPO_URL")
    if not repo:
        raise RuntimeError("APPROVALS_REPO_URL is not set")  # Fail closed: no repo, no approval.
    output = git_call("ls-remote", repo, "refs/heads/main")
    lines = output.splitlines()
    if len(lines) != 1:
        raise RuntimeError("Could not resolve origin/main")
    parts = lines[0].split()
    if len(parts) != 2 or parts[1] != "refs/heads/main" or not SHA_RE.fullmatch(parts[0]):
        raise RuntimeError("Invalid origin/main resolution")
    return parts[0]


def render_upload(p):
    text = (f"Tester upload: expected build +{p['build_number_expected']}\n"
            f"Last confirmed uploaded: +{p['last_uploaded_build']}\n"
            f"Main: {p['sha']}\nApp commits: {p['app_commits']}")
    for pr in p.get("prs", []):
        text += f"\n#{pr['number']}: {pr['title']}"
    if p.get("local_build_max", 0) > p["last_uploaded_build"]:
        text += f"\nlocal build dirs up to +{p['local_build_max']} exist (not confirmed uploaded)"
    return text


def upload_precheck(record, ctx):
    try:
        current = ctx.main_sha()
    except Exception:
        return False, "verify_failed", "could not verify main, try again"
    if current != record["payload"]["sha"]:
        return False, "main_moved", "main moved — a fresh request will come"
    return True, "ok", ""


@dataclasses.dataclass(frozen=True)
class Kind:
    name: str
    render: object
    precheck: object
    runner: object
    risk: object
    area: str


def runner_argv(name, record):
    default = Path(__file__).resolve().parents[1] / "bin"
    directory = Path(os.environ.get("APPROVALS_RUNNER_DIR", str(default))).expanduser()
    return [str(directory / ("approval-runner-" + name)), "--", record["id"]]


class RiskBlockIncomplete(ValueError):
    def __init__(self, fields):
        self.fields = sorted(set(fields))
        super().__init__('Risk block incomplete: ' + ', '.join(self.fields))


def validate_risk(block, repeat):
    fields = []
    block = block if isinstance(block, dict) else {}
    for name, allowed in (('production_risk', ('none', 'low', 'medium', 'high')),
                          ('reaches', ('nobody', 'testers', 'some users', 'all users'))):
        if block.get(name) not in allowed:
            fields.append(name)
    for name in ('risk_reason', 'blast_radius', 'undo') + (('diff_from_last',) if repeat or 'diff_from_last' in block else ()):
        if not isinstance(block.get(name), str) or not block[name].strip():
            fields.append(name)
    evidence = block.get('evidence')
    if not isinstance(evidence, list) or not evidence or any(
            not isinstance(e, dict) or not isinstance(e.get('item'), str) or not e['item'].strip()
            or 'count' not in e or (e['count'] is not None and (type(e['count']) is not int or e['count'] < 0))
            for e in evidence):
        fields.append('evidence')
    if fields:
        raise RiskBlockIncomplete(fields)


def upload_risk(record, ctx):
    p = record['payload']
    diff = f"since +{p['last_uploaded_build']}: {p['app_commits']} app commits, {len(p.get('prs', []))} PRs"
    same = [r for r in ctx['previous'] if r['payload'].get('sha') == p['sha']]
    if same:
        earlier = max(same, key=lambda r: r['created_at'])
        diff += '; same SHA as an earlier request that ' + earlier['status']
    return dict(production_risk='low',
                risk_reason=f"tester-only build of main @ {p['sha'][:8]}; nothing reaches end users",
                reaches='testers',
                blast_radius='internal testers get a broken build until the next upload',
                undo='Cannot be recalled once uploaded; the tester build can be expired in the store console.',
                evidence=[dict(item='PRs included', count=len(p.get('prs', []))),
                          dict(item='app commits', count=p['app_commits'])], diff_from_last=diff)


def echo_risk(record, ctx):
    return dict(production_risk='none', risk_reason='test message only', reaches='nobody',
                blast_radius='none (test message only)', undo='nothing to undo',
                evidence=[dict(item='test kind — no gates', count=None)], diff_from_last='test kind')


def render_risk(block):
    evidence = '; '.join(e['item'] + (f" ({e['count']})" if e['count'] is not None else '')
                         for e in block['evidence'])
    text = (f"RISK: {block['production_risk']} — {block['risk_reason']}\nReaches: {block['reaches']}"
            f"\nIf wrong: {block['blast_radius']}\nUndo: {block['undo']}\nEvidence: {evidence}")
    if block.get('diff_from_last'):
        text += '\nSince last time: ' + block['diff_from_last']
    return text


# Request kinds. "tester-upload" is an example of a real action; its runner in bin/
# is a stub that does nothing. Add your own kind here with its render, precheck,
# runner and risk functions.
KINDS = {
    "tester-upload": Kind("tester-upload", render_upload, upload_precheck,
                          lambda r: runner_argv("tester-upload", r), upload_risk, "mobile-app"),
    "test-echo": Kind("test-echo", lambda p: "Test echo approval\n" + p.get("note", ""),
                      lambda r, c: (True, "ok", ""), lambda r: runner_argv("test-echo", r), echo_risk, "test"),
}


def render_record(record, limit=3800):
    prefix = "[TEST] " if is_test(record) else ""
    # Historical records may predate risk blocks; they may be edited, never re-sent unchecked.
    risk = '\n\n' + render_risk(record['risk']) if record.get('risk') else ''
    p = record['payload']
    prs = p.get('prs', []) if record['kind'] == 'tester-upload' else []
    for count in range(len(prs), -1, -1):
        payload = dict(p, prs=prs[:count]) if prs else p
        summary = prefix + KINDS[record['kind']].render(payload)
        if count < len(prs):
            summary += f'\n… and {len(prs) - count} more'
        text = summary + risk
        if len(text) <= limit:
            return text
    # A complete fixed block/summary cannot be made safe by cutting its content.
    raise RiskBlockIncomplete(['risk' if len(risk) > limit else 'summary'])


EDIT_SUFFIX_BUDGET = 250


def check_request_risk(record, ctx):
    try:
        validate_risk(record.get('risk'), record.get('risk_repeat', False))
        render_record(record, limit=3800 - EDIT_SUFFIX_BUDGET)
        return True
    except RiskBlockIncomplete as error:
        audit('risk_block_incomplete', id=record['id'], fields=error.fields)
        decide(record['id'], 'expired', 'risk_block_incomplete', ctx.clock(), ctx=ctx)
        alert('risk_block_incomplete', 'Approval risk block incomplete: ' + ', '.join(error.fields),
              ctx, req_id=record['id'])
        return False


def write_ledger(row, ctx=None):
    try:
        ledger.append(row)
    except Exception:
        # Telemetry must never undo or prevent an owner decision, even on a full disk.
        try:
            audit('ledger_write_failed', id=row['id'], ledger_event=row['event'])
        except Exception:
            print('Could not audit ledger failure', file=sys.stderr)
        if ctx is not None:
            try:
                alert('ledger_write_failed', 'Approval ledger write failed; check local audit.', ctx, req_id=row['id'])
            except Exception:
                print('Could not deliver ledger failure alert', file=sys.stderr)
        else:
            try:
                atomic_write_json(paths().state / 'ledger-alert-pending.json', {'id': row['id']})
            except Exception:
                print('Could not queue ledger failure alert', file=sys.stderr)


def flush_ledger_alert(ctx):
    path = paths().state / 'ledger-alert-pending.json'
    if path.exists():
        pending = json.loads(path.read_text())
        if pending:
            alert('ledger_write_failed', 'Approval ledger write failed; check local audit.', ctx, req_id=pending['id'])
            atomic_write_json(path, {})


def requested_ledger(record, ctx=None):
    kind = KINDS[record['kind']]
    risk = record['risk']
    write_ledger(dict(event='requested', event_key=record['id'] + ':requested', id=record['id'],
                      kind=record['kind'], area=kind.area, risk=risk['production_risk'],
                      lane=f"{record['kind']}|{kind.area}|{risk['production_risk']}",
                      evidence=risk['evidence'], is_test=bool(is_test(record)),
                      created_at=record['created_at'], sent_at=record['sent_at'],
                      reask_of=record.get('reask_of'), **{field: None for field in ledger.OUTCOME_FIELDS}), ctx)


def owner_choice(event):
    if event['reason'] in OWNER_REASONS or event['status'] == 'cancel_requested':
        return 'yes' if event['status'] == 'approved' else 'no'
    return None


def decision_ledger(record, event, ctx=None):
    choices = [e for e in record['history'] if e['version'] <= event['version'] and owner_choice(e)]
    row = dict(id=record['id'], event_key=f"{record['id']}:v{event['version']}")
    if owner_choice(event):
        first = choices[0]
        changed = any(owner_choice(e) != owner_choice(first) for e in choices)
        sent_at = record.get('sent_at')
        row.update(event='decided', first_decision=owner_choice(first), final_decision=owner_choice(event),
                   changed=changed, time_to_answer_s=(stamp(first['at']) - stamp(sent_at)).total_seconds() if sent_at else None,
                   decided_at=event['at'])
    elif event['status'] in ('expired', 'superseded') and not choices:
        row.update(event='superseded' if event['reason'] == 'superseded' else 'expired',
                   at=event['at'], reason=event['reason'])
    else:
        return
    write_ledger(row, ctx)


class TelegramError(RuntimeError):
    def __init__(self, code=None):
        self.code = code
        super().__init__("Telegram request failed" + (f" (HTTP {code})" if type(code) is int else ""))


def stdin_token():
    token = sys.stdin.readline().rstrip("\r\n")
    if not token:
        raise RuntimeError("token unavailable on stdin")
    _SECRETS.add(token)
    return token


def bot_token():
    token = os.environ.get("APPROVALS_TG_TOKEN")
    if not token:
        raise RuntimeError("APPROVALS_TG_TOKEN is not set")
    _SECRETS.add(token)
    return token


class Telegram:
    def __init__(self, config, token=None, transport=None):
        self.chat_id = config["chat_id"]
        self.token = bot_token() if token is None else token
        _SECRETS.update((self.token, str(self.chat_id)))
        self.transport = transport or urllib.request.urlopen

    def _post(self, method, body, timeout=20):
        try:
            request = urllib.request.Request("https://api.telegram.org/bot" + self.token + "/" + method,
                                             data=json.dumps(body).encode(),
                                             headers={"Content-Type": "application/json"}, method="POST")
            with self.transport(request, timeout=timeout) as response:
                result = json.load(response)
            if not result.get("ok"):
                raise TelegramError(result.get("error_code"))
            return result["result"]
        except urllib.error.HTTPError as error:
            raise TelegramError(error.code) from None
        except TelegramError:
            raise
        except Exception:
            # Never expose urllib exception messages, URLs, response bodies, or IDs.
            raise TelegramError() from None

    def send_message(self, text, buttons=None):
        body = dict(chat_id=self.chat_id, text=text[:3800])
        if buttons:
            body["reply_markup"] = {"inline_keyboard": buttons}
        return self._post("sendMessage", body)["message_id"]

    def edit_message(self, message_id, text):
        return self._post("editMessageText", dict(chat_id=self.chat_id, message_id=message_id,
                          text=text[:3800], reply_markup={"inline_keyboard": []}))

    def answer_callback(self, cb_id, text):
        return self._post("answerCallbackQuery", dict(callback_query_id=cb_id, text=text[:200], show_alert=True))

    def get_updates(self, offset, timeout):
        return self._post("getUpdates", dict(offset=offset, timeout=timeout, allowed_updates=["callback_query"]), timeout + 10)


def heartbeat(name, status, code=0):
    """Optional: APPROVALS_HEARTBEAT names a command called as `<cmd> <name> <status> <code>`."""
    command = os.environ.get("APPROVALS_HEARTBEAT", "")
    if command:
        try:
            subprocess.run([command, name, status, str(code)], check=True, capture_output=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            audit("heartbeat_failed", name=name, status=status)
            print("Approval heartbeat failed", file=sys.stderr)


def launch_runner(argv, log_path, token):
    fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "ab") as log:
        os.fchmod(log.fileno(), 0o600)
        # The token goes over stdin only, never argv or the child's environment.
        process = subprocess.Popen([argv[0], "--token-stdin", *argv[1:]], start_new_session=True, stdin=subprocess.PIPE, stdout=log, stderr=log,
                                   env={k: v for k, v in os.environ.items() if k != "APPROVALS_TG_TOKEN"})
        try:
            process.stdin.write((token + "\n").encode())
        finally:
            process.stdin.close()
    audit("runner_launched", pid=process.pid)
    return process.pid


@dataclasses.dataclass
class Context:
    config: dict
    telegram: object
    clock: object = utcnow
    main_sha: object = main_sha
    launcher: object = launch_runner


def context(token=None):
    config = load_config()  # Fail before token retrieval or network if not configured.
    return Context(config, Telegram(config, token=token))


def telegram_send(ctx, text, buttons=None, *, purpose, req_id=None):
    message = ctx.telegram.send_message(text, buttons)
    audit("telegram_sent", purpose=purpose, id=req_id)
    return message


def alert(key, text, ctx, req_id=None):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
        raise ValueError("Invalid alert key")
    path = paths().state / "alerts" / key
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            now = ctx.clock().timestamp()
            if os.fstat(lock.fileno()).st_size and now - os.fstat(lock.fileno()).st_mtime < 3600:
                return
            # Reserve before send, preventing repeated failures from flooding logs/API.
            lock.seek(0)
            lock.write("alert\n")
            lock.flush()
            os.utime(path, (now, now))
            telegram_send(ctx, scrub(text), purpose="alert", req_id=req_id)
        audit("alert_sent", key=key)
    except Exception:
        print("Approval alert delivery failed", file=sys.stderr)
        try:
            audit("alert_failed", key=key)
        except Exception:
            print("Could not write approval alert audit", file=sys.stderr)


def edit_decision(record, ctx, detail=""):
    if record.get("message_id") is None:
        return
    local = stamp(record["decided_at"]).astimezone(local_tz())
    reason = record["decided_reason"]
    attribution = " by owner" if reason == "owner" else " (" + {
        "timeout": "timeout", "main_moved": "main moved",
        "superseded": "superseded", "send_failed": "send failed",
    }.get(reason, reason) + ")"
    suffix = f"\n\n{record['status'].upper()}{attribution} at {local:%H:%M %Z}"
    if detail:
        suffix += "\n" + detail
    try:
        text = render_record(record, limit=3800 - max(EDIT_SUFFIX_BUDGET, len(suffix))) + suffix
        ctx.telegram.edit_message(record["message_id"], text)
        audit("message_edited", id=record["id"])
    except Exception:
        audit("message_edit_failed", id=record["id"])
        alert("message_edit", "Could not update an approval message", ctx, req_id=record["id"])


def build_title(record):
    p = record["payload"]
    return f"build +{p['build_number_expected']} ({p['sha'][:8]})"


def decision_text(record, disabled=False):
    prefix = "[TEST] " if is_test(record) else ""
    ref = f"(request …{record['id'][-6:]})"
    title = "the test echo" if record["kind"] == "test-echo" else build_title(record)
    if record["status"] == "approved":
        delay = grace_seconds()
        duration = f"{delay // 60} minutes" if delay % 60 == 0 else f"{delay} seconds"
        verb = "Running" if record["kind"] == "test-echo" else "Starting"
        target = title if record["kind"] == "test-echo" else f"uploading {title} to testers"
        detail = f"{verb} in {duration}." if not record["launched"] else f"{verb} now."
        if record["payload"].get("test") and record["kind"] == "tester-upload":
            detail += " Test request — dry run only."
        return f"{prefix}You said YES to {target}. {detail} {ref}"
    if record["status"] == "declined":
        return f"{prefix}You said NO to {title}. Nothing will be {'run' if record['kind'] == 'test-echo' else 'built'}. {ref}"
    if record["status"] == "cancel_requested":
        return f"{prefix}You said NO to {title}. {cancel_detail(record)} {ref}"
    reason = "main moved; a fresh request will come." if record["decided_reason"] == "main_moved" else "request expired."
    return f"{prefix}⚠️ Not approved — {reason} {title} {ref}"


def cancel_detail(record):
    started = paths().state / "runs" / (record["id"] + ".started")
    if started.exists():
        return "Too late to cancel: the run already started. The runner will stop before its next step if it can."
    return "Cancellation requested; the runner will stop before its next step."


def confirm(record, ctx, text=None):
    purpose = "confirmation" if record["decided_reason"] in OWNER_REASONS else "outcome"
    # Serialize the message handoff with transitions. A slow send cannot replace a newer live message.
    with store_lock():
        current = _load_request(record["id"])
        if current["version"] != record["version"]:
            # The accepted choice still merits a phone notification, even if the
            # runner finished before delivery. Never attach obsolete live buttons.
            notify(record, (text or decision_text(record)) +
                   "\nSuperseded by latest outcome: " + current["decided_reason"], ctx, purpose=purpose)
            return
        previous = current.get("live_message_id")
        buttons = None
        if current["status"] in ("approved", "declined") and current["decided_reason"] != "runner_disabled":
            action, label = ("n", "Change to No") if current["status"] == "approved" else ("y", "Change to Yes")
            buttons = [[dict(text=label, callback_data=callback_data(current["id"], action, current["version"]))]]
        try:
            current["live_message_id"] = telegram_send(ctx, text or decision_text(current), buttons, purpose=purpose, req_id=current["id"])
            atomic_write_json(paths().requests / (current["id"] + ".json"), current)
        except Exception:
            audit("confirmation_send_failed", id=record["id"])
            alert("confirmation_send_failed", "Could not deliver approval notification; check local audit", ctx, req_id=record["id"])
            return
        if previous is not None and previous != current.get("message_id"):
            try:
                ctx.telegram.edit_message(previous, "superseded — use the latest message")
            except Exception:
                audit("message_edit_failed", id=record["id"])
        edit_decision(current, ctx)


def notify(record, text, ctx, event="confirmation_send_failed", purpose="outcome"):
    try:
        telegram_send(ctx, text, purpose=purpose, req_id=record["id"])
    except Exception:
        audit(event, id=record["id"])
        alert(event, "Could not deliver approval notification; check local audit", ctx, req_id=record["id"])


def scan_orphans(ctx):
    for record in all_requests():
        req_id = record["id"]
        marker = paths().state / "alerts" / ("orphan_" + req_id)
        if (record["kind"] == "tester-upload" and record["status"] == "approved"
                and record.get("launched", False)
                and not (paths().state / "runs" / (req_id + ".log")).exists() and not marker.exists()):
            try:
                telegram_send(ctx, f"Approved but never launched: {req_id}; run approval-runner-tester-upload -- {req_id}", purpose="alert", req_id=req_id)
                atomic_write_json(marker, True)
            except Exception:
                audit("orphan_alert_failed", id=req_id)


def handle_update(update, ctx):
    cb = update.get("callback_query")
    if not isinstance(cb, dict):
        return
    reply = "could not handle request"
    try:
        sender = cb.get("from") or {}
        message = cb.get("message") or {}
        chat = message.get("chat") or {}
        from_hash = hash_id(sender.get("id"))
        reason = None
        # Authorise before parsing or touching the store.
        if sender.get("id") != ctx.config["owner_user_id"]:
            reason = "wrong_owner"
        elif chat.get("id") != ctx.config["chat_id"]:
            reason = "wrong_chat"
        elif chat.get("type") != "private":
            reason = "non_private"
        if reason:
            reply = "not authorised"
            audit("callback_rejected", from_hash=from_hash, reason=reason)
            return
        audit("callback_authorised", from_hash=from_hash)
        try:
            req_id, action, version = parse_callback(cb.get("data"))
            record = load_request(req_id)
        except (ValueError, FileNotFoundError):
            reply = "unknown request"
            audit("callback_rejected", from_hash=from_hash, reason="unknown_request")
            return
        if version != record["version"] or message.get("message_id") != record["live_message_id"]:
            reply = "superseded — use the latest message"
            audit("callback_rejected", id=req_id, reason="superseded")
            return
        allowed = {"pending": ("a", "s"), "approved": ("n",), "declined": ("y",)}
        if action not in allowed.get(record["status"], ()):
            reply = f"already handled ({record['status']})"
            audit("callback_already_handled", id=req_id)
            return
        now = ctx.clock()
        if record["status"] in ("pending", "declined") and now >= stamp(record["expires_at"]):
            won, record = transition(req_id, version, "expired", "timeout", now, ctx=ctx)
            reply = "expired — a fresh request will come" if won else "superseded — use the latest message"
            if won:
                confirm(record, ctx)
            return
        if action in ("a", "y"):
            ok, code, msg = KINDS[record["kind"]].precheck(record, ctx)
            audit("precheck", id=req_id, code=code)
            if not ok:
                reply = msg + (" — a fresh request will come" if action == "y" and "fresh request" not in msg else "")
                if code == "verify_failed":
                    alert("verify_failed", "Could not verify a precheck; please retry", ctx, req_id=req_id)
                elif action == "a":
                    won, record = transition(req_id, version, "expired", code, now, ctx=ctx)
                    if won:
                        confirm(record, ctx)
                return
        status = "approved" if action in ("a", "y") else "cancel_requested" if record["launched"] else "declined"
        reason = "owner" if action in ("a", "s") else "changed to YES" if action == "y" else "changed to NO"
        changes = {"launch_at": iso(now + dt.timedelta(seconds=grace_seconds()))} if status == "approved" else {"launch_at": None}
        won, record = transition(req_id, version, status, reason, now, ctx=ctx, **changes)
        if not won:
            reply = "superseded — use the latest message"
            audit("callback_rejected", id=req_id, reason="superseded")
            return
        reply = cancel_detail(record) if status == "cancel_requested" else status
        confirm(record, ctx)
    finally:
        try:
            ctx.telegram.answer_callback(cb.get("id", ""), reply)
            audit("callback_answered")
        except Exception:
            audit("callback_answer_failed")


def sweep_expired(ctx, now):
    for path in sorted(paths().requests.glob("*.json")):
        if not ID_RE.fullmatch(path.stem) or path.with_suffix(".claim").exists():
            continue
        record = load_request(path.stem)
        if record["status"] in ("pending", "declined") and now >= stamp(record["expires_at"]):
            won, record = transition(record["id"], record["version"], "expired", "timeout", now, ctx=ctx)
            if won:
                confirm(record, ctx)
                audit("request_swept", id=record["id"])


def send_request(kind, payload, ctx):
    record = create_request(kind, payload, ctx.clock())
    if not check_request_risk(record, ctx):
        return load_request(record["id"])
    buttons = [[{"text": "Yes", "callback_data": callback_data(record["id"], "a")},
                {"text": "No", "callback_data": callback_data(record["id"], "s")}]]
    try:
        message_id = telegram_send(ctx, render_record(record), buttons, purpose="request", req_id=record["id"])
        record = set_message_id(record["id"], message_id, ctx.clock(), ctx=ctx)
    except Exception:
        audit("request_send_failed", id=record["id"])
        decide(record["id"], "expired", "send_failed", ctx.clock(), ctx=ctx)
        raise RuntimeError("Could not send approval request") from None
    audit("request_sent", id=record["id"])
    return record


def process_batch(updates, ctx, handler=handle_update):
    offset = None
    for update in updates:
        try:
            handler(update, ctx)
        except Exception:
            audit("error", component="listener", reason="update_failed")
            alert("update_failed", "An approval update failed; check local audit", ctx)
        offset = update["update_id"] + 1
        atomic_write_json(paths().state / "offset", offset)
    return offset


def listener(ctx, once=False, max_seconds=None, sleep=time.sleep, monotonic=time.monotonic):
    fd = os.open(paths().state / "listener.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "r+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("listener already running")
            return 0
        flush_ledger_alert(ctx)
        launch_due(ctx)
        scan_orphans(ctx)
        stopped = False
        def stop(signum, frame):
            nonlocal stopped
            stopped = True
        old = signal.signal(signal.SIGTERM, stop)
        start = monotonic()
        last_success = None
        failures = 0
        offset_path = paths().state / "offset"
        offset = json.loads(offset_path.read_text()) if offset_path.exists() else 0
        try:
            while not stopped and (max_seconds is None or monotonic() - start < max_seconds):
                flush_ledger_alert(ctx)
                sweep_expired(ctx, ctx.clock())
                launch_due(ctx)
                timeout = 0 if once else poll_timeout(ctx)
                if max_seconds is not None:
                    timeout = min(timeout, max(0, int(max_seconds - (monotonic() - start) - 10)))
                try:
                    updates = ctx.telegram.get_updates(offset, timeout)
                except TelegramError as error:
                    failures += 1
                    audit("error", component="listener", reason="poll_failed", code=error.code)
                    last_success = None
                    if error.code == 409:
                        alert("poll_conflict", "Telegram poll conflict: another poller or webhook is active", ctx)
                        heartbeat("approval-listener", "fail", 3)
                        return 3
                    if failures >= 5:
                        alert("poll_failed", "Approval polling has failed five times", ctx)
                        heartbeat("approval-listener", "fail", error.code or 1)
                    if once:
                        heartbeat("approval-listener", "fail", 1)
                        return 1
                    delay = min(5 * 2 ** min(failures - 1, 4), 60)
                    deadline = monotonic() + delay
                    while not stopped and monotonic() < deadline:
                        if max_seconds is not None and monotonic() - start >= max_seconds:
                            break
                        launch_due(ctx)
                        sleep(min(1, deadline - monotonic()))
                    continue
                failures = 0
                current = monotonic()
                if last_success is None or current - last_success >= 300:
                    heartbeat("approval-listener", "success")
                    last_success = current
                new_offset = process_batch(updates, ctx)
                if new_offset is not None:
                    offset = new_offset
                if once:
                    break
        finally:
            signal.signal(signal.SIGTERM, old)
    return 0


def poll_timeout(ctx):
    remaining = [(stamp(r['launch_at']) - ctx.clock()).total_seconds() for r in all_requests()
                 if r['status'] == 'approved' and not r['launched'] and r['launch_at']]
    return max(0, min(50, int(min(remaining, default=50))))


def launch_due(ctx):
    notifications = []
    for record in all_requests():
        if (record['status'] != 'approved' or record['launched'] or not record['launch_at']
                or stamp(record['launch_at']) > ctx.clock()):
            continue
        # Real (non-test) runs need an explicit on-switch file; absent means nothing runs.
        disabled = (record['kind'] != 'test-echo' and not is_test(record)
                    and not (paths().state / (record['kind'] + '.enabled')).exists())
        won, record = transition(record['id'], record['version'], 'approved',
                                 'runner_disabled' if disabled else 'launch', ctx.clock(), launched=True)
        if not won:
            continue
        log = paths().state / 'runs' / (record['id'] + '.log')
        if disabled:
            durable_line(log, 'runner disabled at launch; nothing run')
            notifications.append((record, f"Approved {build_title(record)} — runner disabled, nothing built.", False))
        else:
            try:
                ctx.launcher(KINDS[record['kind']].runner(record), log, ctx.telegram.token)
            except Exception:
                audit('launch_failed', id=record['id'])
                notifications.append((record, 'launch failed; manual recovery required', True))
                continue
            notifications.append((record, None, False))
    for record, text, failed in notifications:
        if failed:
            runner_outcome(record, ctx, text)
            alert('launch_failed', 'Approved request could not launch; check local audit', ctx, req_id=record['id'])
        else:
            confirm(record, ctx, text)


def runner_outcome(record, ctx, reason, status='finished'):
    while True:
        current = load_request(record['id'])
        won, current = transition(current['id'], current['version'], status, reason, ctx.clock(), runner_done=True)
        if won:
            break
    if current.get('live_message_id') is not None:
        try:
            ctx.telegram.edit_message(current['live_message_id'], reason)
        except Exception:
            audit('message_edit_failed', id=current['id'])
    edit_decision(current, ctx, reason)


def request_main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--test", action="store_true")
    parser.add_argument("kind", nargs="?", choices=KINDS)
    parser.add_argument("--note", default="")
    parser.add_argument("--sha")
    parser.add_argument("--build", type=int)
    parser.add_argument("--last-build", type=int)
    parser.add_argument("--pr", action="append", default=[])
    parser.add_argument("--app-commits", type=int, default=0)
    args = parser.parse_args()
    load_config()
    if args.list:
        for r in all_requests():
            print(r["id"], r["kind"], r["status"], r["created_at"])
        return 0
    if not args.kind:
        parser.error("kind is required unless --list is used")
    payload = {"note": args.note, "test": args.test}
    if args.kind == "tester-upload":
        if not args.sha or not SHA_RE.fullmatch(args.sha) or args.build is None or args.last_build is None:
            parser.error("tester-upload requires a full --sha, --build and --last-build")
        if args.build < 1 or args.last_build < 0 or args.app_commits < 0:
            parser.error("build and commit counts must be nonnegative; build must be positive")
        prs = []
        for entry_text in args.pr:
            number, sep, title = entry_text.partition("|")
            if not sep or not number.isdecimal():
                parser.error('--pr must be "1234|title"')
            prs.append(dict(number=int(number), title=title))
        payload = dict(sha=args.sha, build_number_expected=args.build, last_uploaded_build=args.last_build,
                       prs=prs, app_commits=args.app_commits, test=args.test)
    print(send_request(args.kind, payload, context())["id"])
    return 0


def listener_main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--max-seconds", type=float)
    args = parser.parse_args()
    if args.max_seconds is not None and args.max_seconds < 0:
        parser.error("--max-seconds must be nonnegative")
    ctx = context()
    try:
        return listener(ctx, args.once, args.max_seconds)
    except Exception:
        audit("error", component="listener", reason="fatal")
        heartbeat("approval-listener", "fail", 1)
        alert("listener_fatal", "Approval listener failed; check the local audit", ctx)
        raise


def upload_main():
    """Example runner for the tester-upload kind. It does nothing real.

    Replace the marked block with your own action. Keep the checks around it:
    the runner refuses anything that is not an approved request of its kind,
    and it honours a cancel that arrived after launch.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("request_id")
    parser.add_argument("--token-stdin", action="store_true")
    args = parser.parse_args()
    load_config()
    record = load_request(args.request_id)
    if record["kind"] != "tester-upload" or record["status"] not in ("approved", "cancel_requested"):
        raise RuntimeError("Upload runner requires an approved tester-upload request")
    ctx = context(stdin_token() if args.token_stdin else None)
    if (paths().state / "runs" / (record["id"] + ".cancel")).exists():
        runner_outcome(record, ctx, "cancelled before upload; nothing uploaded")
        return 0
    durable_line(paths().state / "runs" / (record["id"] + ".started"), "started", once=True)
    # --- EXAMPLE: put the real, approved action here. ---
    audit("example_runner_ran", id=record["id"])
    runner_outcome(record, ctx, ("[TEST] " if is_test(record) else "") + "example runner finished; nothing uploaded")
    return 0


def echo_main():
    parser = argparse.ArgumentParser()
    parser.add_argument("request_id")
    parser.add_argument("--token-stdin", action="store_true")
    args = parser.parse_args()
    load_config()
    record = load_request(args.request_id)
    if record["kind"] != "test-echo" or record["status"] not in ("approved", "cancel_requested"):
        raise RuntimeError("Test echo runner requires an approved test-echo request")
    ctx = context(stdin_token() if args.token_stdin else None)
    if (paths().state / "runs" / (record["id"] + ".cancel")).exists():
        runner_outcome(record, ctx, "[TEST] test echo cancelled")
        return 0
    audit("test_echo_ran", id=record["id"])
    notify(record, f"✅ Test echo ran (request …{record['id'][-6:]}).", ctx, "runner_notification_failed")
    runner_outcome(record, ctx, "[TEST] test echo finished")
    print(record["id"])
    return 0


def entry(main):
    os.umask(0o077)
    try:
        return main()
    except Exception as error:
        print(scrub(str(error)), file=sys.stderr)
        return 1
