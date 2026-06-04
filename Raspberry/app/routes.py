import json
from flask import Blueprint, render_template, request, jsonify, current_app
from . import database

main_bp = Blueprint('main', __name__)


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
    current_app.extensions['mqtt'].set_valve(data['box'], data['valve'], data['state'])
    return jsonify({'ok': True})


@main_bp.route('/api/pump', methods=['POST'])
def control_pump():
    data = request.get_json()
    if not data or 'state' not in data:
        return jsonify({'error': 'missing field: state'}), 400
    current_app.extensions['mqtt'].set_pump(data['state'])
    return jsonify({'ok': True})


@main_bp.route('/api/all_off', methods=['POST'])
def all_off():
    current_app.extensions['scheduler'].stop_script()
    current_app.extensions['mqtt'].all_off()
    return jsonify({'ok': True})


@main_bp.route('/api/state')
def get_state():
    return jsonify(current_app.extensions['mqtt'].get_state())


# ---------- Scripts ----------

@main_bp.route('/scripts')
def scripts():
    return render_template('scripts.html', scripts=database.get_scripts())


@main_bp.route('/api/scripts', methods=['GET'])
def api_scripts():
    return jsonify(database.get_scripts())


@main_bp.route('/api/scripts', methods=['POST'])
def api_save_script():
    data = request.get_json()
    if not data or 'name' not in data or 'steps' not in data:
        return jsonify({'error': 'missing fields: name, steps'}), 400
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
    current_app.extensions['scheduler'].run_script(script_id)
    return jsonify({'ok': True})


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
    sched_id = database.save_schedule(
        data['name'], data['script_id'], data['cron'],
        data.get('enabled', True),
    )
    current_app.extensions['scheduler'].reload_schedule(sched_id)
    return jsonify({'id': sched_id})


@main_bp.route('/api/schedules/<int:sched_id>', methods=['PUT'])
def api_update_schedule(sched_id):
    data = request.get_json()
    if not data or 'name' not in data or 'script_id' not in data or 'cron' not in data:
        return jsonify({'error': 'missing fields: name, script_id, cron'}), 400
    database.update_schedule(
        sched_id, data['name'], data['script_id'], data['cron'],
        data.get('enabled', True),
    )
    current_app.extensions['scheduler'].reload_schedule(sched_id)
    return jsonify({'ok': True})


@main_bp.route('/api/schedules/<int:sched_id>', methods=['DELETE'])
def api_delete_schedule(sched_id):
    database.delete_schedule(sched_id)
    current_app.extensions['scheduler'].remove_schedule(sched_id)
    return jsonify({'ok': True})


# ---------- Log ----------

@main_bp.route('/log')
def log():
    logs = database.get_logs(200)
    return render_template('log.html', logs=logs)


@main_bp.route('/api/log')
def api_log():
    return jsonify(database.get_logs(200))


@main_bp.route('/api/scheduler/status')
def scheduler_status():
    return jsonify(current_app.extensions['scheduler'].get_status())
