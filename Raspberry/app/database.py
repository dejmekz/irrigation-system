import sqlite3
import json
from pathlib import Path
from typing import Any

DB_PATH = str(Path(__file__).parent.parent / 'irrigation.db')


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    # Enable WAL mode first (cannot run inside executescript)  Fix #14
    conn = get_db()
    conn.execute('PRAGMA journal_mode=WAL')
    conn.commit()
    conn.close()

    with get_db() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS scripts (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT    NOT NULL,
                steps      TEXT    NOT NULL,
                pump_box   INTEGER,
                pump_delay INTEGER DEFAULT 0,
                created_at TEXT    DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS schedules (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT    NOT NULL,
                script_id  INTEGER NOT NULL,
                cron       TEXT    NOT NULL,
                enabled    INTEGER DEFAULT 1,
                FOREIGN KEY (script_id) REFERENCES scripts(id)
            );
            CREATE TABLE IF NOT EXISTS message_log (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                topic     TEXT NOT NULL,
                payload   TEXT NOT NULL,
                direction TEXT NOT NULL,
                ts        TEXT DEFAULT (datetime('now'))
            );
        ''')
        # Migrate existing DB: add pump columns if missing
        for col, defn in [('pump_box', 'INTEGER'), ('pump_delay', 'INTEGER DEFAULT 0')]:
            try:
                conn.execute(f'ALTER TABLE scripts ADD COLUMN {col} {defn}')
            except Exception:
                pass


def log_message(topic: str, payload: str, direction: str):
    try:                                             # Fix #14: don't crash MQTT callback on DB error
        with get_db() as conn:
            conn.execute(
                'INSERT INTO message_log (topic, payload, direction) VALUES (?, ?, ?)',
                (topic, str(payload), direction),
            )
            conn.execute(
                'DELETE FROM message_log WHERE id NOT IN '
                '(SELECT id FROM message_log ORDER BY id DESC LIMIT 1000)'
            )
    except Exception:
        pass


def get_logs(limit: int = 200):
    with get_db() as conn:
        rows = conn.execute(
            'SELECT * FROM message_log ORDER BY id DESC LIMIT ?', (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_scripts():
    with get_db() as conn:
        rows = conn.execute('SELECT * FROM scripts ORDER BY name').fetchall()
    return [dict(r) for r in rows]


def get_script(script_id: int):
    with get_db() as conn:
        row = conn.execute(
            'SELECT * FROM scripts WHERE id = ?', (script_id,)
        ).fetchone()
    return dict(row) if row else None


def save_script(name: str, steps: list[dict[str, Any]], pump_box: int | None = None, pump_delay: int = 0):
    with get_db() as conn:
        cur = conn.execute(
            'INSERT INTO scripts (name, steps, pump_box, pump_delay) VALUES (?, ?, ?, ?)',
            (name, json.dumps(steps), pump_box, pump_delay),
        )
        return cur.lastrowid


def update_script(script_id: int, name: str, steps: list[dict[str, Any]], pump_box: int | None = None, pump_delay: int = 0):
    with get_db() as conn:
        conn.execute(
            'UPDATE scripts SET name=?, steps=?, pump_box=?, pump_delay=? WHERE id=?',
            (name, json.dumps(steps), pump_box, pump_delay, script_id),
        )


def delete_script(script_id: int):
    with get_db() as conn:
        conn.execute('DELETE FROM scripts WHERE id = ?', (script_id,))
        conn.execute('DELETE FROM schedules WHERE script_id = ?', (script_id,))


def get_schedules():
    with get_db() as conn:
        rows = conn.execute('''
            SELECT s.id, s.name, s.script_id, s.cron, s.enabled,
                   sc.name AS script_name
            FROM schedules s
            JOIN scripts sc ON s.script_id = sc.id
            ORDER BY s.name
        ''').fetchall()
    return [dict(r) for r in rows]


def save_schedule(name: str, script_id: int, cron: str, enabled: bool = True):
    with get_db() as conn:
        cur = conn.execute(
            'INSERT INTO schedules (name, script_id, cron, enabled) VALUES (?, ?, ?, ?)',
            (name, script_id, cron, int(enabled)),
        )
        return cur.lastrowid


def update_schedule(sched_id: int, name: str, script_id: int, cron: str, enabled: bool = True):
    with get_db() as conn:
        conn.execute(
            'UPDATE schedules SET name=?, script_id=?, cron=?, enabled=? WHERE id=?',
            (name, script_id, cron, int(enabled), sched_id),
        )


def delete_schedule(sched_id: int):
    with get_db() as conn:
        conn.execute('DELETE FROM schedules WHERE id = ?', (sched_id,))
