"""Storage-layer behaviour that the API tests cannot see."""
import pytest


def test_message_log_is_capped(db):
    for i in range(1200):
        db.log_message('t/topic', f'payload {i}', 'out')
    with db.get_db() as conn:
        count = conn.execute('SELECT COUNT(*) FROM message_log').fetchone()[0]
    # Trimming happens every _TRIM_EVERY inserts rather than on each one, so the
    # table sits at the cap plus at most one trim interval.
    assert 1000 <= count <= 1000 + db._TRIM_EVERY
    with db.get_db() as conn:
        newest = conn.execute(
            'SELECT payload FROM message_log ORDER BY id DESC LIMIT 1').fetchone()[0]
    assert newest == 'payload 1199'


def test_run_history_is_capped(db):
    for i in range(db._RUN_HISTORY_LIMIT + 120):
        db.record_run(None, f'run {i}', 'manual', 'completed')
    with db.get_db() as conn:
        count = conn.execute('SELECT COUNT(*) FROM run_history').fetchone()[0]
    assert db._RUN_HISTORY_LIMIT <= count <= db._RUN_HISTORY_LIMIT + db._TRIM_EVERY


def test_log_write_failure_never_raises(db, monkeypatch):
    """Bookkeeping must not be able to kill the thread that closes valves."""
    monkeypatch.setattr(db, 'get_db', lambda: (_ for _ in ()).throw(RuntimeError('disk')))
    db.log_message('t', 'p', 'out')          # must not raise
    assert db.record_run(None, 'x', 'manual', 'error') is None


def test_foreign_keys_are_enforced(db):
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        db.save_schedule('orphan', 9999, '0 6 * * *')


def test_script_exists_rejects_non_integers(db):
    script_id = db.save_script('x', [])
    assert db.script_exists(script_id) is True
    assert db.script_exists(script_id + 1) is False
    for bad in ('1', None, True, 1.0):
        assert db.script_exists(bad) is False


def test_timestamps_are_converted_to_local_on_read(db):
    db.log_message('t', 'p', 'out')
    row = db.get_logs(1)[0]
    assert row['ts'] and len(row['ts']) == 19
