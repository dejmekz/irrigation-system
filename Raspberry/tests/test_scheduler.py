"""Run outcomes. The through-line: a run may only be recorded as 'completed'
when commands actually went out and the controller was there to receive them."""
import pytest

from conftest import FakeMQTT
from helpers import STEPS, wait_for_run


@pytest.fixture
def script(db):
    return db.save_script('test', STEPS)


def run(db, mqtt, sio, script, **kwargs):
    from app.scheduler import IrrigationScheduler

    scheduler = IrrigationScheduler(mqtt, sio, **kwargs)
    result = scheduler.run_script(script)
    wait_for_run(scheduler)
    return db.get_runs(1)[0], result


def test_healthy_run_completes(db, mqtt, sio, script):
    row, result = run(db, mqtt, sio, script)
    assert result['ok'] is True
    assert row['outcome'] == 'completed'
    assert not row['detail']
    assert mqtt.sent == [
        ('valve', 1, 1, True), ('valve', 1, 1, False),
        ('valve', 2, 3, True), ('valve', 2, 3, False),
    ]


def test_failed_publishes_are_not_a_success(db, sio, script):
    """With the broker down every publish fails, and the run used to finish as
    'completed' having watered nothing."""
    mqtt = FakeMQTT(publish_ok=False)
    row, _ = run(db, mqtt, sio, script)
    assert row['outcome'] == 'error'
    assert 'never left the client' in row['detail']


def test_offline_controller_aborts_before_publishing(db, sio, script):
    mqtt = FakeMQTT(online=False)
    row, result = run(db, mqtt, sio, script)
    assert result == {'ok': False, 'reason': 'offline', 'error': result['error']}
    assert row['outcome'] == 'offline'
    assert 'offline' in row['detail'].lower()
    assert mqtt.sent == []


def test_unknown_controller_status_is_not_treated_as_online(db, sio, script):
    mqtt = FakeMQTT(online=None)
    row, _ = run(db, mqtt, sio, script)
    assert row['outcome'] == 'offline'
    assert 'unknown' in row['detail'].lower()
    assert mqtt.sent == []


def test_missing_script_is_reported(db, mqtt, sio):
    from app.scheduler import IrrigationScheduler

    result = IrrigationScheduler(mqtt, sio).run_script(9999)
    assert result['reason'] == 'not_found'
    assert db.get_runs(1)[0]['outcome'] == 'error'


def test_second_run_is_blocked_and_recorded(db, mqtt, sio, script):
    from app.scheduler import IrrigationScheduler

    scheduler = IrrigationScheduler(mqtt, sio)
    assert scheduler.run_script(script)['ok'] is True
    blocked = scheduler.run_script(script)
    assert blocked['reason'] == 'blocked'
    wait_for_run(scheduler)
    outcomes = [r['outcome'] for r in db.get_runs(2)]
    assert 'blocked' in outcomes


# --- controller lost partway through a run ---

def test_long_outage_mid_run_fails_the_run(db, sio, script):
    mqtt = FakeMQTT(offline_from=2)          # online for the start check only
    row, _ = run(db, mqtt, sio, script, offline_grace=1)
    assert row['outcome'] == 'error'
    assert 'unreachable for' in row['detail']
    assert 'while valves were open' in row['detail']


def test_long_outage_is_logged_once_not_every_second(db, sio, script):
    mqtt = FakeMQTT(offline_from=2)
    run(db, mqtt, sio, script, offline_grace=1)
    assert len(sio.logs('system/offline')) == 1


def test_brief_outage_stays_completed_but_is_recorded(db, sio, script):
    mqtt = FakeMQTT(offline_from=2, offline_to=2)     # a single sample
    row, _ = run(db, mqtt, sio, script, offline_grace=1)
    assert row['outcome'] == 'completed'
    assert 'briefly unreachable' in row['detail']


def test_outage_with_no_valve_open_omits_the_valve_clause(db, sio, script):
    # Sample 3 lands during the wait step, with nothing open.
    mqtt = FakeMQTT(offline_from=3, offline_to=3)
    row, _ = run(db, mqtt, sio, script, offline_grace=0)
    assert row['outcome'] == 'error'
    assert 'while valves were open' not in row['detail']


def test_outage_and_failed_publishes_are_both_reported(db, sio, script):
    mqtt = FakeMQTT(offline_from=2, publish_ok=False)
    row, _ = run(db, mqtt, sio, script, offline_grace=1)
    assert row['outcome'] == 'error'
    assert 'never left the client' in row['detail']
    assert 'unreachable for' in row['detail']


def test_manual_stop_keeps_its_reason(db, sio, script):
    from app.scheduler import IrrigationScheduler

    holder = {}

    class StopOnFirstValve(FakeMQTT):
        def set_valve(self, box, valve, on):
            if not self.sent:
                holder['scheduler'].stop_script()
            return super().set_valve(box, valve, on)

    scheduler = IrrigationScheduler(StopOnFirstValve(), sio, offline_grace=0)
    holder['scheduler'] = scheduler
    scheduler.run_script(script)
    wait_for_run(scheduler)
    row = db.get_runs(1)[0]
    assert row['outcome'] == 'stopped'
    assert 'stopped manually' in row['detail']


def test_timed_pump_on_closes_the_pump(db, mqtt, sio):
    """A timed pump_on used to run the pump for its duration and leave it on."""
    script = db.save_script('pump', [{'action': 'pump_on', 'duration': 1}])
    run(db, mqtt, sio, script)
    assert ('pump', False) in mqtt.sent


def test_untimed_valve_on_stays_open_until_the_script_ends(db, mqtt, sio):
    script = db.save_script('untimed', [
        {'action': 'valve_on', 'box': 1, 'valve': 1, 'duration': 0},
        {'action': 'wait', 'duration': 1},
    ])
    run(db, mqtt, sio, script)
    # Opened once, and closed by the end-of-script cleanup rather than early.
    assert mqtt.sent == [('valve', 1, 1, True), ('valve', 1, 1, False)]


# --- outputs nobody is managing ---

def test_unmanaged_valve_is_closed_after_the_timeout(db, mqtt, sio):
    from app.scheduler import IrrigationScheduler

    scheduler = IrrigationScheduler(mqtt, sio, manual_timeout=0)
    scheduler._manual_timeout = 1
    mqtt.state = {'pump': 'OFF', '1': {'valves': {'1': 'ON'}}}

    scheduler._check_unmanaged()          # first sighting starts the clock
    assert mqtt.sent == []
    scheduler._unmanaged_since[(1, 1)] -= 5   # pretend it has been on a while
    scheduler._check_unmanaged()
    assert ('valve', 1, 1, False) in mqtt.sent
    assert len(sio.logs('system/manual_timeout')) == 1


def test_valve_a_script_opened_is_left_alone(db, mqtt, sio):
    from app.scheduler import IrrigationScheduler

    scheduler = IrrigationScheduler(mqtt, sio, manual_timeout=0)
    scheduler._manual_timeout = 1
    scheduler._open_valves.add((1, 1))
    mqtt.state = {'pump': 'OFF', '1': {'valves': {'1': 'ON'}}}

    scheduler._check_unmanaged()
    assert scheduler._unmanaged_since == {}
    scheduler._check_unmanaged()
    assert mqtt.sent == []


def test_timer_resets_once_the_valve_goes_off(db, mqtt, sio):
    from app.scheduler import IrrigationScheduler

    scheduler = IrrigationScheduler(mqtt, sio, manual_timeout=0)
    scheduler._manual_timeout = 1
    mqtt.state = {'pump': 'OFF', '1': {'valves': {'1': 'ON'}}}
    scheduler._check_unmanaged()
    mqtt.state = {'pump': 'OFF', '1': {'valves': {'1': 'OFF'}}}
    scheduler._check_unmanaged()
    assert scheduler._unmanaged_since == {}
