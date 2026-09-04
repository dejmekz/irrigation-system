import json
import os
from flask import Blueprint, send_from_directory, request, jsonify, current_app

firmware_bp = Blueprint('firmware', __name__)

# Fallback for local development when no directory is configured.
_DEFAULT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'firmware')


def _cfg() -> dict:
    return current_app.config.get('FW_CFG') or {}


def firmware_dir() -> str:
    """Where irrigation.bin and manifest.json live. In production this is a
    directory served statically by Apache: the Flask dev server truncates a 1 MB
    download to a slow client like the ESP32, which fails the OTA image check."""
    return _cfg().get('dir') or _DEFAULT_DIR


def manifest_path() -> str:
    return os.path.join(firmware_dir(), 'manifest.json')


@firmware_bp.route('/firmware/manifest.json')
def manifest():
    try:
        with open(manifest_path()) as f:
            return jsonify(json.load(f))
    except FileNotFoundError:
        return jsonify({'error': 'manifest not found'}), 404


@firmware_bp.route('/firmware/irrigation.bin')
def binary():
    # Kept as a fallback only. The manifest points the ESP32 at the static
    # server instead, so this path is not used by a normal OTA.
    return send_from_directory(firmware_dir(), 'irrigation.bin')


@firmware_bp.route('/firmware/upload', methods=['POST'])
def upload():
    if 'firmware' not in request.files:
        return jsonify({'error': 'missing field: firmware'}), 400
    f = request.files['firmware']

    cfg = _cfg()
    fw_dir = firmware_dir()
    os.makedirs(fw_dir, exist_ok=True)

    # Write via a temp file and rename: the static server reads this directory
    # directly, so an in-flight OTA must never see a half-written image.
    target = os.path.join(fw_dir, 'irrigation.bin')
    tmp = target + '.part'
    f.save(tmp)
    os.replace(tmp, target)

    try:
        with open(manifest_path()) as mf:
            data = json.load(mf)
    except (FileNotFoundError, ValueError):
        data = {'type': 'irrigation-esp32c3', 'version': 0,
                'bin': '/firmware/irrigation.bin'}

    # esp32FOTA only flashes when the manifest version exceeds the version the
    # device is running. A manifest that is missing (it is deliberately not in
    # git — it is server-side state) or that has fallen behind the device would
    # let uploads succeed while no OTA ever triggers, with nothing to show for
    # it. Floor the version at whatever the ESP32 last reported over MQTT.
    device_fw = 0
    try:
        device_fw = int(current_app.extensions['mqtt'].get_state().get('fw') or 0)
    except Exception:
        pass
    data['version'] = max(int(data.get('version', 0)), device_fw) + 1
    # Always re-assert where the binary is actually served from, so the manifest
    # cannot drift away from the configured static host.
    data['host'] = cfg.get('host', 'raspi4server.local')
    data['port'] = int(cfg.get('port', 80))

    tmp_manifest = manifest_path() + '.part'
    with open(tmp_manifest, 'w') as mf:
        json.dump(data, mf, indent=2)
    os.replace(tmp_manifest, manifest_path())

    return jsonify({'ok': True, 'version': data['version'],
                    'host': data['host'], 'port': data['port']})


@firmware_bp.route('/firmware/trigger', methods=['POST'])
def trigger():
    current_app.extensions['mqtt'].publish('irrigation/cmd/ota_update', 'trigger')
    return jsonify({'ok': True})
