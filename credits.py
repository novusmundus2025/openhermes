"""Credit ledger: integer microcredits, immutable references, owner-only awards."""
import argparse
from decimal import Decimal
import json
from pathlib import Path
import time

SCALE = 1_000_000


def units(value):
    number = Decimal(str(value)) * SCALE
    if not number.is_finite() or number < 0 or number != number.to_integral_value():
        raise ValueError('Credits must be nonnegative with at most six decimal places')
    return int(number)


def schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS credit_ledger (
        reference TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(user_id),
        delta_microunits INTEGER NOT NULL, kind TEXT NOT NULL,
        reason TEXT NOT NULL, created_at INTEGER NOT NULL)''')
    db.execute('CREATE INDEX IF NOT EXISTS credit_ledger_user_idx ON credit_ledger(user_id)')
    db.execute('''CREATE TABLE IF NOT EXISTS user_credit_roles (
        user_id TEXT PRIMARY KEY REFERENCES users(user_id), is_contributor INTEGER NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS credit_rates (
        version TEXT PRIMARY KEY, definition TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS contributor_devices (
        device_id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(user_id))''')


def entry(db, user_id, delta, kind, reason, reference):
    schema(db)
    if not reference or not reason:
        raise ValueError('A reason and unique reference are required')
    if not db.execute('SELECT 1 FROM users WHERE user_id=?', (user_id,)).fetchone():
        raise ValueError('User must first authenticate and use the integration')
    old = db.execute('SELECT user_id,delta_microunits,kind,reason FROM credit_ledger WHERE reference=?', (reference,)).fetchone()
    values = (user_id, delta, kind, reason)
    if old:
        if tuple(old) != values:
            raise ValueError('Reference already used for a different transaction')
        return
    db.execute('INSERT INTO credit_ledger VALUES (?,?,?,?,?,?)',
               (reference, *values, int(time.time())))


def award(db, user_id, amount, kind, reason, reference):
    schema(db)
    if kind not in ('grant', 'contribution'):
        raise ValueError('Invalid award type')
    if kind == 'contribution':
        role = db.execute('SELECT is_contributor FROM user_credit_roles WHERE user_id=?', (user_id,)).fetchone()
        if not role or not role[0]:
            raise ValueError('This user is not marked as a contributor')
    entry(db, user_id, units(amount), kind, reason, reference)


def account(db, user_id):
    schema(db)
    row = db.execute('''SELECT coalesce(sum(delta_microunits),0),
        coalesce(sum(CASE WHEN kind='contribution' THEN delta_microunits ELSE 0 END),0),
        coalesce(sum(CASE WHEN kind='grant' THEN delta_microunits ELSE 0 END),0),
        coalesce(-sum(CASE WHEN kind='usage' THEN delta_microunits ELSE 0 END),0)
        FROM credit_ledger WHERE user_id=?''', (user_id,)).fetchone()
    return dict(zip(('balance_microunits', 'earned_microunits', 'granted_microunits', 'spent_microunits'), row))


def initial(db, user_id, policy):
    if policy and 'initial_credits' in policy:
        schema(db)
        if not db.execute('SELECT 1 FROM credit_ledger WHERE reference=?', ('initial:' + user_id,)).fetchone():
            award(db, user_id, policy['initial_credits'], 'grant', 'Initial account credits', 'initial:' + user_id)


def settle(db, request_id, policy):
    if not policy or policy.get('formula') != 'tokens100' or not policy.get('version'):
        return
    schema(db)
    version = str(policy['version'])
    definition = 'ceil((input_tokens+output_tokens)/100)'
    if policy.get('fallback_formula') == 'chars400':
        definition += '; fallback=max(1,ceil((prompt_chars+output_chars)/400))'
    old = db.execute('SELECT definition FROM credit_rates WHERE version=?', (version,)).fetchone()
    if old and old[0] != definition:
        raise ValueError('Credit rate version is immutable; use a new version')
    db.execute('INSERT OR IGNORE INTO credit_rates VALUES (?,?)', (version, definition))
    row = db.execute('''SELECT user_id,prompt_tokens,completion_tokens,credits_microunits,status,prompt_chars,output_chars
        FROM usage_events WHERE request_id=?''', (request_id,)).fetchone()
    if not row or row[3] is not None or row[4] not in ('completed', 'usage_unreported'):
        return
    if row[1] is not None and row[2] is not None:
        cost = ((row[1] + row[2] + 99) // 100) * SCALE
        source = 'runtime_tokens'
    elif policy.get('fallback_formula') == 'chars400' and row[5] is not None and row[6] is not None:
        cost = max(1, (row[5] + row[6] + 399) // 400) * SCALE
        source = 'character_estimate'
    else:
        return
    entry(db, row[0], -cost, 'usage', 'Model usage at rate ' + version, 'usage:' + request_id)
    db.execute('UPDATE usage_events SET credits_microunits=?,credit_rate_version=?,billing_source=? WHERE request_id=?',
               (cost, version, source, request_id))


def main():
    parser = argparse.ArgumentParser(description='Owner-only user credit administration')
    parser.add_argument('command', choices=('show', 'grant', 'earn', 'set-contributor', 'link-device', 'settle', 'sync'))
    parser.add_argument('--user-id')
    parser.add_argument('--amount')
    parser.add_argument('--reason')
    parser.add_argument('--reference')
    parser.add_argument('--device-id')
    args = parser.parse_args()
    from server import connect
    if args.command == 'sync':
        from contributions import sync
        config = json.loads((Path(__file__).parent/'settings.json').read_text())
        print(json.dumps(sync(connect, config.get('contribution_source'))))
        return
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        schema(db)
        if args.command == 'settle':
            from usage import schema as usage_schema
            usage_schema(db)
            policy = json.loads((Path(__file__).parent/'settings.json').read_text()).get('credits')
            if not policy:
                parser.error('Configure credit pricing in settings.json first')
            for (rid,) in db.execute('SELECT request_id FROM usage_events WHERE credits_microunits IS NULL').fetchall():
                settle(db, rid, policy)
        else:
            if not args.user_id:
                parser.error('--user-id is required')
            if args.command in ('set-contributor', 'link-device'):
                if not db.execute('SELECT 1 FROM users WHERE user_id=?', (args.user_id,)).fetchone():
                    parser.error('Unknown user')
                db.execute('INSERT INTO user_credit_roles VALUES (?,1) ON CONFLICT(user_id) DO UPDATE SET is_contributor=1', (args.user_id,))
                if args.command == 'link-device':
                    if not args.device_id:
                        parser.error('--device-id is required')
                    old = db.execute('SELECT user_id FROM contributor_devices WHERE device_id=?', (args.device_id,)).fetchone()
                    if old and old[0] != args.user_id:
                        parser.error('Device is already owned by another account')
                    db.execute('INSERT OR IGNORE INTO contributor_devices VALUES (?,?)', (args.device_id, args.user_id))
            elif args.command in ('grant', 'earn'):
                if args.amount is None or not args.reason or not args.reference:
                    parser.error('--amount, --reason and --reference are required')
                award(db, args.user_id, args.amount, 'contribution' if args.command == 'earn' else 'grant', args.reason, 'award:' + args.reference)
            print(json.dumps(account(db, args.user_id)))


if __name__ == '__main__':
    main()
