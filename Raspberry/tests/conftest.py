"""Shared fixtures. Everything here runs against a stub MQTT client and a
throwaway SQLite file — no broker, no ESP32, no valves."""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


@pytest.fixture
def db(monkeypatch):
    """A fresh database per test. app.database resolves DB_PATH at call time,
    so pointing it at a temp file is enough to isolate a test."""
    import app.database as database

    path = tempfile.NamedTemporaryFile(suffix='.db', delete=False).name
    monkeypatch.setattr(database, 'DB_PATH', path)
    database.init_db()
    yield database
    for suffix in ('', '-wal', '-shm'):
        try:
            os.unlink(path + suffix)
        except OSError:
            pass


class FakeSocketIO:
    def __init__(self):
        self.events = []

    def emit(self, name, data=None, *args, **kwargs):
        self.events.append((name, data))

    def logs(self, topic):
        return [d for n, d in self.events
                if n == 'log_entry' and (d or {}).get('topic') == topic]


class FakeMQTT:
    """Records what was published and reports a settable controller status.

    `offline_from`/`offline_to` are indexes into the esp32_status() call
    sequence, which lets a test drop the controller partway through a run: call
    1 is the check run_script makes before starting, and each later call is one
    of the per-second samples the scheduler takes while a script sleeps.
    """

    def __init__(self, online=True, publish_ok=True,
                 offline_from=None, offline_to=10 ** 9):
        self.online = online
        self.publish_ok = publish_ok
        self.offline_from = offline_from
        self.offline_to = offline_to
        self.calls = 0
        self.sent = []
        self.state = {'pump': 'OFF'}

    def esp32_status(self):
        self.calls += 1
        if self.offline_from is not None:
            return not (self.offline_from <= self.calls <= self.offline_to)
        return self.online

    def set_valve(self, box, valve, on):
        self.sent.append(('valve', box, valve, on))
        return self.publish_ok

    def set_pump(self, on):
        self.sent.append(('pump', on))
        return self.publish_ok

    def all_off(self):
        self.sent.append(('all_off',))
        return self.publish_ok

    def get_state(self):
        return dict(self.state)

    # --- only used by the Flask app fixture ---
    connected = True
    on_esp32_online = None

    def connect(self):
        pass

    def subscribe_gate(self, topic):
        pass

    def get_gate_states(self):
        return {}


@pytest.fixture
def sio():
    return FakeSocketIO()


@pytest.fixture
def mqtt():
    return FakeMQTT()


@pytest.fixture
def client(db, monkeypatch):
    """Flask test client with the MQTT client stubbed out before create_app."""
    import app.mqtt_client as mqtt_module

    fake = FakeMQTT()
    monkeypatch.setattr(mqtt_module, 'MQTTClient', lambda *a, **kw: fake)
    monkeypatch.setenv('SECRET_KEY', 'test')

    from app import create_app

    config = os.path.join(os.path.dirname(__file__), '..', 'config.yaml')
    application = create_app(config)
    application.extensions['mqtt'] = fake
    application.config['TESTING'] = True
    test_client = application.test_client()
    test_client.mqtt = fake
    yield test_client
    application.extensions['scheduler']._scheduler.shutdown(wait=False)
