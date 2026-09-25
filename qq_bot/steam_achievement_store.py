"""Durable baselines and per-group notification outbox for Steam achievements."""
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class AchievementStore:
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
                CREATE TABLE IF NOT EXISTS progress (
                  sid TEXT, app TEXT, complete INTEGER, updated REAL, PRIMARY KEY(sid,app));
                CREATE TABLE IF NOT EXISTS events (
                  sid TEXT, app TEXT, payload TEXT, PRIMARY KEY(sid,app));
                CREATE TABLE IF NOT EXISTS deliveries (
                  sid TEXT, app TEXT, gid TEXT, state TEXT, attempts INTEGER DEFAULT 0,
                  retry_at REAL DEFAULT 0, PRIMARY KEY(sid,app,gid));
                CREATE TABLE IF NOT EXISTS schedules (
                  key TEXT PRIMARY KEY, due REAL, failures INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS schemas (
                  app TEXT PRIMARY KEY, names TEXT, updated REAL);
                CREATE TABLE IF NOT EXISTS targets (
                  sid TEXT, app TEXT, title TEXT, expires REAL, PRIMARY KEY(sid,app));
                ''')
                yield db
        finally:
            db.close()

    def targets(self, now):
        with self.db() as db:
            db.execute('DELETE FROM targets WHERE expires<?', (now,))
            return [dict(row) for row in db.execute('SELECT * FROM targets')]

    def target(self, sid, app, title, now):
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO targets VALUES (?,?,?,?)', (sid, app, title, now + 14 * 86400))

    def due(self, key, now):
        with self.db() as db:
            row = db.execute('SELECT due FROM schedules WHERE key=?', (key,)).fetchone()
            return row is None or row['due'] <= now

    def expedite(self, key, now):
        with self.db() as db:
            # Switching a recent game into active play must not retain its
            # 30-minute schedule. Preserve network-failure backoff, however.
            db.execute('UPDATE schedules SET due=MIN(due,?) WHERE key=? AND failures=0', (now + 300, key))

    def schedule(self, key, now, interval, failed=False):
        with self.db() as db:
            row = db.execute('SELECT failures FROM schedules WHERE key=?', (key,)).fetchone()
            failures = min((row['failures'] if row else 0) + 1, 7) if failed else 0
            delay = min(60 * 2 ** failures, 3600) if failed else interval
            db.execute('INSERT OR REPLACE INTO schedules VALUES (?,?,?)', (key, now + delay, failures))

    def suppress(self, key, now, interval=7 * 86400):
        """Retry unreadable or unsupported achievement data infrequently."""
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO schedules VALUES (?,?,?)', (key, now + interval, -1))

    def schema(self, app, now):
        with self.db() as db:
            row = db.execute('SELECT * FROM schemas WHERE app=?', (app,)).fetchone()
            return set(json.loads(row['names'])) if row and now - row['updated'] < 86400 else None

    def save_schema(self, app, names, now):
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO schemas VALUES (?,?,?)', (app, json.dumps(sorted(names)), now))

    def observe(self, sid, app, complete, payload, groups, now):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT complete FROM progress WHERE sid=? AND app=?', (sid, app)).fetchone()
            existed = db.execute('SELECT 1 FROM events WHERE sid=? AND app=?', (sid, app)).fetchone()
            created = bool(previous and not previous['complete'] and complete and not existed)
            if created:
                db.execute('INSERT INTO events VALUES (?,?,?)', (sid, app, json.dumps(payload)))
                db.executemany('INSERT OR IGNORE INTO deliveries(sid,app,gid,state) VALUES (?,?,?,?)',
                               [(sid, app, gid, 'pending') for gid in groups])
            # A game already complete on first sight is permanently consumed.
            if previous is None and complete and not existed:
                db.execute('INSERT INTO events VALUES (?,?,?)', (sid, app, json.dumps(payload)))
            db.execute('INSERT OR REPLACE INTO progress VALUES (?,?,?,?)', (sid, app, int(complete), now))
            return created

    def pending(self, now):
        with self.db() as db:
            rows = db.execute('''SELECT d.*,e.payload FROM deliveries d JOIN events e USING(sid,app)
                WHERE state='pending' AND retry_at<=? AND attempts<3 LIMIT 100''', (now,)).fetchall()
            return [dict(row, payload=json.loads(row['payload'])) for row in rows]

    def claim(self, sid, app, gid):
        with self.db() as db:
            return db.execute("UPDATE deliveries SET state='uncertain',attempts=attempts+1 WHERE sid=? AND app=? AND gid=? AND state='pending'",
                              (sid, app, gid)).rowcount == 1

    def finish(self, sid, app, gid, state, now):
        with self.db() as db:
            db.execute('UPDATE deliveries SET state=?,retry_at=? WHERE sid=? AND app=? AND gid=?',
                       (state, now + 60, sid, app, gid))

    def cancel_removed(self, active):
        with self.db() as db:
            for row in db.execute("SELECT sid,app,gid FROM deliveries WHERE state='pending'").fetchall():
                if (row['sid'], row['gid']) not in active:
                    db.execute("UPDATE deliveries SET state='cancelled' WHERE sid=? AND app=? AND gid=?", tuple(row))
