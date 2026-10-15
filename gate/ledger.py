"""Append-only track record. No approval or readiness policy lives here."""
import datetime as dt
import fcntl
import json
import os
from pathlib import Path

OUTCOME_FIELDS = ('reverted', 'post_merge_failure', 'regression_7d', 'regression_28d')


def append(row):
    state = Path(os.environ.get('APPROVALS_STATE_DIR', '~/.agent-state/approvals')).expanduser()
    fd = os.open(state / 'ledger.jsonl', os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, 'a+') as out:
        fcntl.flock(out, fcntl.LOCK_EX)
        os.fchmod(out.fileno(), 0o600)
        # Replay after a claim/JSON crash must not duplicate a logical event.
        if row.get('event_key'):
            out.seek(0)
            if any(json.loads(line).get('event_key') == row['event_key'] for line in out):
                return
        out.write(json.dumps(row, ensure_ascii=False) + '\n')
        out.flush()
        os.fsync(out.fileno())


def record_outcome(id, field, value, at):
    """Future verifier schema: event, id, field, boolean value, observation at (UTC)."""
    if field not in OUTCOME_FIELDS or type(value) is not bool:
        raise ValueError('Invalid outcome field or value')
    append(dict(event='outcome', id=id, field=field, value=value,
                at=at.astimezone(dt.timezone.utc).isoformat()))
