"""HTTP surface: what the browser is told, and what reaches the database."""
import pytest


# --- controller reachability ---

def test_esp32_endpoint_reports_each_state(client):
    for value in (True, False, None):
        client.mqtt.online = value
        assert client.get('/api/esp32').get_json() == {'online': value}


def test_valve_command_is_accepted_while_online(client):
    assert client.post('/api/valve', json={'box': 1, 'valve': 1, 'state': True}).status_code == 200


@pytest.mark.parametrize('status', [False, None])
def test_commands_are_refused_while_the_controller_is_unreachable(client, status):
    client.mqtt.online = status
    before = len(client.mqtt.sent)
    response = client.post('/api/valve', json={'box': 1, 'valve': 1, 'state': True})
    assert response.status_code == 503
    assert response.get_json()['error']
    assert len(client.mqtt.sent) == before
    assert client.post('/api/pump', json={'state': True}).status_code == 503


def test_emergency_stop_is_never_blocked(client):
    """It should always try, even with nothing listening."""
    client.mqtt.online = False
    assert client.post('/api/all_off').status_code == 200
    assert ('all_off',) in client.mqtt.sent


@pytest.mark.parametrize('payload,field', [
    ({'valve': 1, 'state': True}, 'box'),
    ({'box': 1, 'state': True}, 'valve'),
    ({'box': 1, 'valve': 1}, 'state'),
    ({'box': 0, 'valve': 1, 'state': True}, 'box'),
    ({'box': 9, 'valve': 1, 'state': True}, 'box'),
    ({'box': 1, 'valve': 7, 'state': True}, 'valve'),
    ({'box': 1, 'valve': 1, 'state': 'true'}, 'state'),
])
def test_bad_valve_requests_are_rejected(client, payload, field):
    response = client.post('/api/valve', json=payload)
    assert response.status_code == 400
    assert field in response.get_json()['error']


# --- scripts ---

def test_script_round_trip(client):
    created = client.post('/api/scripts', json={
        'name': 'ok', 'steps': [{'action': 'valve_on', 'box': 1, 'valve': 1,
                                 'duration': 600}]})
    assert created.status_code == 200
    script_id = created.get_json()['id']
    assert any(s['id'] == script_id for s in client.get('/api/scripts').get_json())


@pytest.mark.parametrize('steps', [
    [{'action': 'valve_on', 'box': 1, 'valve': 1, 'duration': 5400}],
    [{'action': 'nope', 'box': 1, 'valve': 1, 'duration': 60}],
    [{'action': 'valve_on', 'box': 99, 'valve': 1, 'duration': 60}],
])
def test_invalid_scripts_are_rejected_on_create_and_update(client, steps):
    assert client.post('/api/scripts', json={'name': 'bad', 'steps': steps}).status_code == 400
    good = client.post('/api/scripts', json={
        'name': 'good', 'steps': [{'action': 'wait', 'duration': 5}]}).get_json()['id']
    assert client.put(f'/api/scripts/{good}',
                      json={'name': 'good', 'steps': steps}).status_code == 400


def test_run_endpoint_reports_a_missing_script(client):
    response = client.post('/api/scripts/4242/run')
    assert response.status_code == 404
    assert response.get_json()['ok'] is False


def test_run_endpoint_reports_an_offline_controller(client):
    script_id = client.post('/api/scripts', json={
        'name': 'x', 'steps': [{'action': 'wait', 'duration': 1}]}).get_json()['id']
    client.mqtt.online = False
    response = client.post(f'/api/scripts/{script_id}/run')
    assert response.status_code == 503
    assert 'offline' in response.get_json()['error'].lower()


# --- schedules ---

def test_schedule_needs_a_real_script(client):
    response = client.post('/api/schedules', json={
        'name': 's', 'script_id': 999, 'cron': '0 6 * * *'})
    assert response.status_code == 400
    assert 'script_id' in response.get_json()['error']
    assert client.get('/api/schedules').get_json() == []


def test_schedule_rejects_a_bad_cron(client):
    script_id = client.post('/api/scripts', json={
        'name': 'x', 'steps': []}).get_json()['id']
    assert client.post('/api/schedules', json={
        'name': 's', 'script_id': script_id, 'cron': 'nonsense'}).status_code == 400


def test_deleting_a_script_removes_its_schedules(client, db):
    """Foreign keys are enforced now, so the delete order matters."""
    script_id = client.post('/api/scripts', json={
        'name': 'x', 'steps': []}).get_json()['id']
    client.post('/api/schedules', json={
        'name': 's', 'script_id': script_id, 'cron': '0 6 * * *'})
    assert len(client.get('/api/schedules').get_json()) == 1
    assert client.delete(f'/api/scripts/{script_id}').status_code == 200
    assert client.get('/api/schedules').get_json() == []
    assert client.get('/api/scripts').get_json() == []


def test_a_saved_schedule_is_visible_in_the_list(client):
    """An unenforced foreign key let a schedule save and then vanish, because
    get_schedules() inner-joins scripts."""
    script_id = client.post('/api/scripts', json={
        'name': 'x', 'steps': []}).get_json()['id']
    created = client.post('/api/schedules', json={
        'name': 'evening', 'script_id': script_id, 'cron': '0 20 * * *'})
    assert created.status_code == 200
    listed = client.get('/api/schedules').get_json()
    assert [s['id'] for s in listed] == [created.get_json()['id']]


# --- pages render ---

@pytest.mark.parametrize('path', ['/', '/scripts', '/schedules', '/runs', '/log'])
@pytest.mark.parametrize('status', [True, False, None])
def test_pages_render_in_every_controller_state(client, path, status):
    client.mqtt.online = status
    assert client.get(path).status_code == 200


def test_dashboard_carries_the_controller_badge(client):
    assert b'esp32-badge' in client.get('/').data
