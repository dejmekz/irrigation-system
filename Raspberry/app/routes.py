import json
import sqlite3
from flask import Blueprint, render_template, request, jsonify, current_app
from apscheduler.triggers.cron import CronTrigger
from . import database

main_bp = Blueprint('main', __name__)

# What run_script() may refuse with, and the status each should answer with.
_RUN_REFUSAL_CODES = {'not_found': 404, 'offline': 503, 'blocked': 409}

# Steps the scheduler actually implements. Anything else used to fall through
# its if/elif chain, still sleep out its duration, and leave the run looking
# like a success while that step did nothing at all.
_STEP_ACTIONS = {'valve_on', 'valve_off', 'wait', 'pump_on', 'pump_off',
                 'parallel_group'}
_SUB_ACTIONS = {'valve_on', 'valve_off', 'pump_on', 'pump_off'}
_VALVE_ACTIONS = {'valve_on', 'valve_off'}


def _dims() -> tuple[int, int]:
    """Box and valve counts, from config rather than hardcoded, so growing the
    system is one edit in config.yaml."""
    cfg = current_app.config['SYS_CFG']
    return int(cfg['boxes']), int(cfg['valves_per_box'])


def _validate_cron(cron: str) -> str | None:
    """Return an error string if cron is invalid, else None."""
    parts = cron.split()
    if len(parts) != 5:
        return 'cron must have exactly 5 fields'
    minute, hour, day, month, dow = parts
    try:
        CronTrigger(minute=minute, hour=hour, day=day, month=month, day_of_week=dow)
    except Exception as exc:
        return f'invalid cron: {exc}'
    return None


def _controller_offline() -> str | None:
    """Return an error string unless the ESP32 is known to be online. Commands
    published while it is offline are silently dropped by the broker, so the UI
    must not report them as accepted."""
    status = current_app.extensions['mqtt'].esp32_status()
    if status is True:
        return None
    if status is None:
        return ('controller status unknown — no irrigation/status message '
                'received yet')
    return 'controller is offline — the command would not reach the valves'


def _validate_action(where: str, act: dict, allowed: set, boxes: int,
                     valves: int) -> str | None:
    action = act.get('action')
    if action not in allowed:
        return (f'{where}: unknown action {action!r} — expected one of '
                f'{", ".join(sorted(allowed))}')
    if action in _VALVE_ACTIONS:
        box, valve = act.get('box', 1), act.get('valve', 1)
        if not (isinstance(box, int) and not isinstance(box, bool)
                and 1 <= box <= boxes):
            return f'{where}: box must be an integer 1-{boxes}'
        if not (isinstance(valve, int) and not isinstance(valve, bool)
                and 1 <= valve <= valves):
            return f'{where}: valve must be an integer 1-{valves}'
    return None


def _validate_steps(steps: list, max_on: int, boxes: int = 4,
                    valves: int = 3) -> str | None:
    """Reject a script that would hold a valve open past the firmware's
    per-valve cap (VALVE_MAX_ON_MS). The firmware closes and latches such a
    valve and refuses to reopen it until an explicit OFF, while the Pi carries
    on and records the run as completed — so the tail of the script silently
    waters nothing. Walks the step list the same way the scheduler executes it,
    tracking how long each valve stays open across steps, because an untimed
    valve_on followed by a long wait hits the cap just as a long timed step does.

    A script's pump start delay is deliberately not modelled: it elapses before
    any valve opens, so it shifts both clocks equally and cannot change how long
    a valve is held.
    """
    if max_on <= 0:
        return None

    elapsed = 0
    open_at: dict[tuple[int, int], int] = {}

    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            return f'step {i + 1} is not an object'
        action = step.get('action')
        try:
            duration = int(step.get('duration') or 0)
        except (TypeError, ValueError):
            return f'step {i + 1}: duration must be a number'
        if duration < 0:
            return f'step {i + 1}: duration must not be negative'

        err = _validate_action(f'step {i + 1}', step, _STEP_ACTIONS, boxes, valves)
        if err:
            return err

        subs = (step.get('actions') or []) if action == 'parallel_group' else [step]
        if action == 'parallel_group' and not subs:
            return f'step {i + 1}: parallel group has no actions'
        for j, sub in enumerate(subs):
            if not isinstance(sub, dict):
                return f'step {i + 1}: parallel action {j + 1} is not an object'
            if action == 'parallel_group':
                err = _validate_action(f'step {i + 1} action {j + 1}', sub,
                                       _SUB_ACTIONS, boxes, valves)
                if err:
                    return err
            key = (sub.get('box', 1), sub.get('valve', 1))
            if sub.get('action') == 'valve_on':
                open_at.setdefault(key, elapsed)
            elif sub.get('action') == 'valve_off':
                open_at.pop(key, None)

        elapsed += duration

        for (box, valve), opened in sorted(open_at.items()):
            held = elapsed - opened
            if held >= max_on:
                return (f'step {i + 1}: valve {box}/{valve} would be held open for '
                        f'{held}s, at or over the {max_on}s limit the controller '
                        f'enforces — it would be closed and latched mid-script')

        # A timed valve_on (or timed parallel group) closes what it opened.
        if duration > 0:
            for sub in subs:
                if isinstance(sub, dict) and sub.get('action') == 'valve_on':
                    open_at.pop((sub.get('box', 1), sub.get('valve', 1)), None)

    return None


@main_bp.route('/')
def dashboard():
    cfg = current_app.config['SYS_CFG']
    boxes = range(1, cfg['boxes'] + 1)
    valves = range(1, cfg['valves_per_box'] + 1)
    state = current_app.extensions['mqtt'].get_state()
    mqtt_connected = current_app.extensions['mqtt'].connected
    return render_template(
        'dashboard.html',
        boxes=boxes, valves=valves,
        state=state, mqtt_connected=mqtt_connected,
    )


@main_bp.route('/api/valve', methods=['POST'])
def control_valve():
    data = request.get_json()
    if not data or 'box' not in data or 'valve' not in data or 'state' not in data:
        return jsonify({'error': 'missing fields: box, valve, state'}), 400
    if not isinstance(data['state'], bool):          # Fix #6: reject non-boolean ("false" string etc.)
        return jsonify({'error': 'state must be a JSON boolean'}), 400
    box, valve = data['box'], data['valve']
    boxes, valves = _dims()
    if not (isinstance(box, int) and 1 <= box <= boxes):  # prevent malformed MQTT topics
        return jsonify({'error': f'box must be an integer 1-{boxes}'}), 400
    if not (isinstance(valve, int) and 1 <= valve <= valves):
        return jsonify({'error': f'valve must be an integer 1-{valves}'}), 400
    offline = _controller_offline()
    if offline:
        return jsonify({'error': offline}), 503
    # 502 when the command never left the MQTT client — never report a valve
    # command as accepted when nothing was actually sent.
    ok = current_app.extensions['mqtt'].set_valve(box, valve, data['state'])
    return jsonify({'ok': ok}), (200 if ok else 502)


@main_bp.route('/api/pump', methods=['POST'])
def control_pump():
    data = request.get_json()
    if not data or 'state' not in data:
        return jsonify({'error': 'missing field: state'}), 400
    if not isinstance(data['state'], bool):          # Fix #6
        return jsonify({'error': 'state must be a JSON boolean'}), 400
    offline = _controller_offline()
    if offline:
        return jsonify({'error': offline}), 503
    ok = current_app.extensions['mqtt'].set_pump(data['state'])
    return jsonify({'ok': ok}), (200 if ok else 502)


@main_bp.route('/api/all_off', methods=['POST'])
def all_off():
    current_app.extensions['scheduler'].stop_script()
    ok = current_app.extensions['mqtt'].all_off()
    return jsonify({'ok': ok}), (200 if ok else 502)


@main_bp.route('/api/state')
def get_state():
    return jsonify(current_app.extensions['mqtt'].get_state())


# ---------- Scripts ----------

@main_bp.route('/scripts')
def scripts():
    return render_template(
        'scripts.html',
        scripts=database.get_scripts(),
        valve_max_on=int(current_app.config['SYS_CFG'].get('valve_max_on', 3600)),
    )


@main_bp.route('/api/scripts', methods=['GET'])
def api_scripts():
    return jsonify(database.get_scripts())


@main_bp.route('/api/scripts', methods=['POST'])
def api_save_script():
    data = request.get_json()
    if not data or 'name' not in data or 'steps' not in data:
        return jsonify({'error': 'missing fields: name, steps'}), 400
    if not isinstance(data['name'], str) or not data['name'].strip():
        return jsonify({'error': 'name must be a non-empty string'}), 400  # Fix #8
    if not isinstance(data['steps'], list):          # Fix #9: prevent double-encoded steps
        return jsonify({'error': 'steps must be a JSON array'}), 400
    boxes, valves = _dims()
    err = _validate_steps(
        data['steps'],
        int(current_app.config['SYS_CFG'].get('valve_max_on', 3600)),
        boxes, valves,
    )
    if err:
        return jsonify({'error': err}), 400
    script_id = database.save_script(
        data['name'], data['steps'],
        pump_box=data.get('pump_box'),
        pump_delay=data.get('pump_delay', 0),
    )
    return jsonify({'id': script_id})


@main_bp.route('/api/scripts/<int:script_id>', methods=['PUT'])
def api_update_script(script_id):
    data = request.get_json()
    if not data or 'name' not in data or 'steps' not in data:
        return jsonify({'error': 'missing fields: name, steps'}), 400
    if not isinstance(data['name'], str) or not data['name'].strip():
        return jsonify({'error': 'name must be a non-empty string'}), 400  # Fix #8
    if not isinstance(data['steps'], list):          # Fix #9
        return jsonify({'error': 'steps must be a JSON array'}), 400
    boxes, valves = _dims()
    err = _validate_steps(
        data['steps'],
        int(current_app.config['SYS_CFG'].get('valve_max_on', 3600)),
        boxes, valves,
    )
    if err:
        return jsonify({'error': err}), 400
    database.update_script(
        script_id, data['name'], data['steps'],
        pump_box=data.get('pump_box'),
        pump_delay=data.get('pump_delay', 0),
    )
    return jsonify({'ok': True})


@main_bp.route('/api/scripts/<int:script_id>', methods=['DELETE'])
def api_delete_script(script_id):
    database.delete_script(script_id)
    return jsonify({'ok': True})


@main_bp.route('/api/scripts/<int:script_id>/run', methods=['POST'])
def api_run_script(script_id : int):
    # This used to answer 200 {'ok': true} unconditionally, so a run refused
    # because another script held the lock, or because the controller was
    # offline, looked identical to one that started.
    result = current_app.extensions['scheduler'].run_script(script_id)
    if result.get('ok'):
        return jsonify(result)
    return jsonify(result), _RUN_REFUSAL_CODES.get(result.get('reason'), 409)


@main_bp.route('/api/scripts/stop', methods=['POST'])
def api_stop_script():
    current_app.extensions['scheduler'].stop_script()
    return jsonify({'ok': True})


# ---------- Schedules ----------

@main_bp.route('/schedules')
def schedules():
    return render_template(
        'schedules.html',
        schedules=database.get_schedules(),
        scripts=database.get_scripts(),
    )


@main_bp.route('/api/schedules', methods=['GET'])
def api_schedules():
    return jsonify(database.get_schedules())


@main_bp.route('/api/schedules', methods=['POST'])
def api_save_schedule():
    data = request.get_json()
    if not data or 'name' not in data or 'script_id' not in data or 'cron' not in data:
        return jsonify({'error': 'missing fields: name, script_id, cron'}), 400
    err = _validate_cron(data['cron'])               # Fix #7: validate before saving
    if err:
        return jsonify({'error': err}), 400
    # get_schedules() inner-joins scripts, so a schedule pointing at a script
    # that does not exist was accepted and then invisible in the very list the
    # UI reads back — undeletable, and never registered with the scheduler.
    if not database.script_exists(data['script_id']):
        return jsonify({'error': 'script_id does not match an existing script'}), 400
    sched_id = database.save_schedule(
        data['name'], data['script_id'], data['cron'],
        data.get('enabled', True),
        gate_topic=(data.get('gate_topic') or '').strip() or None,
        gate_payload=(data.get('gate_payload') or '').strip() or 'ON',
    )
    current_app.extensions['scheduler'].reload_schedule(sched_id)
    return jsonify({'id': sched_id})


@main_bp.route('/api/schedules/<int:sched_id>', methods=['PUT'])
def api_update_schedule(sched_id):
    data = request.get_json()
    if not data or 'name' not in data or 'script_id' not in data or 'cron' not in data:
        return jsonify({'error': 'missing fields: name, script_id, cron'}), 400
    err = _validate_cron(data['cron'])               # Fix #7
    if err:
        return jsonify({'error': err}), 400
    if not database.script_exists(data['script_id']):
        return jsonify({'error': 'script_id does not match an existing script'}), 400
    database.update_schedule(
        sched_id, data['name'], data['script_id'], data['cron'],
        data.get('enabled', True),
        gate_topic=(data.get('gate_topic') or '').strip() or None,
        gate_payload=(data.get('gate_payload') or '').strip() or 'ON',
    )
    current_app.extensions['scheduler'].reload_schedule(sched_id)
    return jsonify({'ok': True})


@main_bp.errorhandler(sqlite3.IntegrityError)
def _integrity_error(exc):
    """Foreign keys are enforced now; a violation is a bad request, not a crash."""
    return jsonify({'error': f'database rejected the change: {exc}'}), 400


@main_bp.route('/api/schedules/<int:sched_id>', methods=['DELETE'])
def api_delete_schedule(sched_id):
    database.delete_schedule(sched_id)
    current_app.extensions['scheduler'].remove_schedule(sched_id)
    return jsonify({'ok': True})


@main_bp.route('/api/gates')
def api_gates():
    return jsonify(current_app.extensions['mqtt'].get_gate_states())


# ---------- Run history ----------

@main_bp.route('/runs')
def runs():
    return render_template('runs.html', runs=database.get_runs(100))


@main_bp.route('/api/runs')
def api_runs():
    return jsonify(database.get_runs(100))


# ---------- Log ----------

@main_bp.route('/log')
def log():
    logs = database.get_logs(200)
    return render_template('log.html', logs=logs)


@main_bp.route('/api/log')
def api_log():
    return jsonify(database.get_logs(200))


@main_bp.route('/api/esp32')
def api_esp32():
    """Controller reachability. true/false once irrigation/status has been seen,
    null if it never has — the navbar badge distinguishes all three."""
    return jsonify({'online': current_app.extensions['mqtt'].esp32_status()})


@main_bp.route('/api/scheduler/status')
def scheduler_status():
    return jsonify(current_app.extensions['scheduler'].get_status())
