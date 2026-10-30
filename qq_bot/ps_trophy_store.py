"""Durable PSN baselines, resumable scans and immutable trophy unlock records."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

KINDS = ('bronze', 'silver', 'gold', 'platinum')
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def trophy_db_path() -> Path:
    path = Path(os.getenv('PS_TROPHY_DB_PATH', '').strip() or 'data/ps-trophies.db').expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


class TrophyStore:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                db.executescript('''
                    CREATE TABLE IF NOT EXISTS accounts (
                        account_id TEXT PRIMARY KEY, counts TEXT, target TEXT,
                        stage TEXT NOT NULL DEFAULT 'idle', offset INTEGER NOT NULL DEFAULT 0,
                        generation INTEGER NOT NULL DEFAULT 0, due REAL NOT NULL DEFAULT 0,
                        retry_at REAL NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
                        last_checked REAL, last_success REAL, last_error TEXT);
                    CREATE TABLE IF NOT EXISTS titles (
                        account_id TEXT, service TEXT, title_id TEXT, platform TEXT,
                        name TEXT, icon_url TEXT, counts TEXT NOT NULL, synced_counts TEXT,
                        updated REAL, synced_updated REAL, generation INTEGER NOT NULL,
                        PRIMARY KEY(account_id, service, title_id));
                    CREATE TABLE IF NOT EXISTS trophies (
                        account_id TEXT, service TEXT, title_id TEXT, trophy_id TEXT,
                        kind TEXT NOT NULL, name TEXT, earned_at REAL NOT NULL,
                        observed_at REAL NOT NULL,
                        PRIMARY KEY(account_id, service, title_id, trophy_id));
                    CREATE INDEX IF NOT EXISTS trophy_unlocks ON trophies(account_id, kind, earned_at);
                ''')
                yield db
        finally:
            db.close()

    def state(self, account: str) -> dict:
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO accounts(account_id) VALUES (?)', (account,))
            return dict(db.execute('SELECT * FROM accounts WHERE account_id=?', (account,)).fetchone())

    def begin(self, account: str, counts: tuple[int, ...], now: float):
        with self.db() as db:
            db.execute('''UPDATE accounts SET target=?, stage='listing', offset=0,
                generation=generation+1, last_checked=?, retry_at=0, last_error=NULL
                WHERE account_id=?''', (json.dumps(counts), now, account))

    def unchanged(self, account: str, now: float, due: float):
        with self.db() as db:
            db.execute('''UPDATE accounts SET last_checked=?, due=?, retry_at=0,
                failures=0, last_error=NULL WHERE account_id=?''', (now, due, account))

    def save_page(self, account: str, values: list[dict], *, offset: int, finished: bool):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            generation = db.execute('SELECT generation FROM accounts WHERE account_id=?', (account,)).fetchone()[0]
            for value in values:
                key = (account, value['service'], value['title_id'])
                previous = db.execute('SELECT counts FROM titles WHERE account_id=? AND service=? AND title_id=?', key).fetchone()
                if previous and any(new < old for new, old in zip(value['counts'], json.loads(previous['counts']))):
                    raise ValueError('Game trophy counts decreased; keeping saved baseline')
                counts = json.dumps(value['counts'])
                db.execute('''INSERT INTO titles VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(account_id,service,title_id) DO UPDATE SET
                    platform=excluded.platform, name=excluded.name, icon_url=excluded.icon_url,
                    counts=excluded.counts, updated=excluded.updated, generation=excluded.generation''',
                    (*key, value['platform'], value['name'], value['icon_url'], counts,
                     counts if not any(value['counts']) else None, value['updated'],
                     value['updated'] if not any(value['counts']) else None, generation))
            db.execute('UPDATE accounts SET offset=?, stage=?, retry_at=0, last_error=NULL WHERE account_id=?',
                       (offset, 'details' if finished else 'listing', account))

    def pending(self, account: str, limit: int) -> list[dict]:
        with self.db() as db:
            return [dict(row) for row in db.execute('''
                SELECT t.* FROM titles t JOIN accounts a USING(account_id)
                WHERE account_id=? AND t.generation=a.generation
                  AND (synced_counts IS NULL OR t.counts!=synced_counts)
                ORDER BY CASE WHEN json_extract(t.counts,'$[3]')>0 THEN 0 ELSE 1 END,
                         updated DESC, service, title_id LIMIT ?''', (account, limit))]

    def save_trophies(self, title: dict, trophies: list[dict], now: float):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            key = (title['account_id'], title['service'], title['title_id'])
            db.executemany('INSERT OR IGNORE INTO trophies VALUES (?,?,?,?,?,?,?,?)',
                [(*key, value['id'], value['kind'], value['name'], value['earned_at'], now) for value in trophies])
            saved = {row['kind']: row['amount'] for row in db.execute('''SELECT kind,COUNT(*) AS amount
                FROM trophies WHERE account_id=? AND service=? AND title_id=? GROUP BY kind''', key)}
            if tuple(saved.get(kind, 0) for kind in KINDS) != tuple(json.loads(title['counts'])):
                raise ValueError('Stored trophy identities disagree with title counts')
            db.execute('UPDATE titles SET synced_counts=?, synced_updated=? WHERE account_id=? AND service=? AND title_id=?',
                       (title['counts'], title['updated'], *key))
            db.execute('UPDATE accounts SET retry_at=0, last_error=NULL WHERE account_id=?', (key[0],))

    def finish(self, account: str, now: float, due: float):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            state = db.execute('SELECT * FROM accounts WHERE account_id=?', (account,)).fetchone()
            target = tuple(json.loads(state['target']))
            totals = [0] * len(KINDS)
            rows = db.execute('SELECT counts,synced_counts FROM titles WHERE account_id=? AND generation=?',
                              (account, state['generation'])).fetchall()
            for row in rows:
                if row['counts'] != row['synced_counts']:
                    raise ValueError('Trophy details are incomplete')
                totals = [a + b for a, b in zip(totals, json.loads(row['counts']))]
            if tuple(totals) != target:
                raise ValueError(f'Visible trophy titles do not match account totals: visible={tuple(totals)} account={target}')
            db.execute('''UPDATE accounts SET counts=target, target=NULL, stage='idle',
                last_success=?, due=?, retry_at=0, failures=0, last_error=NULL WHERE account_id=?''',
                (now, due, account))

    def failed(self, account: str, now: float, message: str, *, restart: bool = False, invalid_data: bool = False):
        with self.db() as db:
            failures = min(db.execute('SELECT failures FROM accounts WHERE account_id=?', (account,)).fetchone()[0] + 1, 7)
            delay = min(60 * 2 ** (failures - 1), 3600)
            # Stable validation failures (hidden titles, missing dates or totals
            # mismatch) cannot be repaired by repeatedly scanning an account.
            # Keep short retries for changing samples, then check twice daily.
            if invalid_data and failures >= 4:
                delay = 12 * 3600
            db.execute('UPDATE accounts SET failures=?, retry_at=?, last_error=? WHERE account_id=?',
                       (failures, now + delay, message, account))
            if restart:
                db.execute("UPDATE accounts SET stage='idle', target=NULL, offset=0, due=0 WHERE account_id=?", (account,))
            return delay


def read_platinum_summary(path: Path, accounts: tuple[str, ...], start: datetime,
                          end: datetime) -> tuple[int, frozenset[str]] | None:
    """Read the last complete local snapshot; never create or migrate a DB here."""
    if not accounts or not path.is_file():
        return None
    db = None
    try:
        db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
        db.row_factory = sqlite3.Row
        count, completed = 0, set()
        for account in accounts:
            state = db.execute('SELECT counts,last_success FROM accounts WHERE account_id=?', (account,)).fetchone()
            if state is None or state['counts'] is None or state['last_success'] is None:
                return None
            # Do not expose a partial refresh as a complete month count.
            rows = db.execute('''SELECT p.earned_at,t.name FROM trophies p JOIN titles t
                USING(account_id,service,title_id) WHERE p.account_id=? AND p.kind='platinum'
                AND p.earned_at<=? AND p.observed_at<=?''',
                (account, end.timestamp(), state['last_success']))
            for row in rows:
                if row['earned_at'] >= start.timestamp():
                    count += 1
                if row['name']:
                    completed.add(' '.join(row['name'].split()).casefold())
        return count, frozenset(completed)
    except (OSError, sqlite3.Error, ValueError):
        return None
    finally:
        if db is not None:
            db.close()
