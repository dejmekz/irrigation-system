import json
import os
from flask import Blueprint, send_from_directory, request, jsonify, current_app

firmware_bp = Blueprint('firmware', __name__)

# Fallback for local development when no directory is configured.
_DEFAULT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'firmware')

# The firmware embeds its FIRMWARE_VERSION as plain text under this marker
# (see FW_VERSION_TAG in ESP32/src/main.cpp).
_VERSION_TAG = b'IRRIGATION_FW_VERSION='
_VERSION_END = b':END'


def version_from_image(path: str) -> int | None:
    """Read FIRMWARE_VERSION out of a built image, or None if the image predates
    the marker. Everywhere else the version exists only as compiled instructions
    and heartbeat arguments, so it cannot be recovered from a binary — which is
    why this endpoint used to keep a counter of its own. That counter tracked
    uploads rather than firmware, so it drifted: after a successful flash the
    manifest sat one ahead of the device, and every trigger re-flashed the same
    image forever."""
    try:
        with open(path, 'rb') as f:
            blob = f.read()
    except OSError:
        return None
    start = blob.find(_VERSION_TAG)
    if start < 0:
        return None
    start += len(_VERSION_TAG)
    end = blob.find(_VERSION_END, start)
    if end < 0 or not 0 < end - start <= 10:
        return None
    try:
        version = int(blob[start:end])
    except ValueError:
        return None
    return version if version > 0 else None


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

    device_fw = 0
    try:
        device_fw = int(current_app.extensions['mqtt'].get_state().get('fw') or 0)
    except Exception:
        pass

    # Prefer the version the image reports about itself, so the manifest states
    # what is actually in the file rather than how many times this endpoint has
    # been called.
    image_version = version_from_image(target)
    if image_version is not None:
        data['version'] = image_version
        version_source = 'image'
    else:
        # An image built before the marker existed. Fall back to the old
        # behaviour: esp32FOTA only flashes when the manifest version exceeds
        # the running one, so a missing manifest (deliberately not in git — it
        # is server-side state) or one that has fallen behind the device would
        # let uploads succeed while no OTA ever fires. Floor it at whatever the
        # ESP32 last reported over MQTT.
        data['version'] = max(int(data.get('version', 0)), device_fw) + 1
        version_source = 'counter'
    # Always re-assert where the binary is actually served from, so the manifest
    # cannot drift away from the configured static host.
    data['host'] = cfg.get('host', 'raspi4server.local')
    data['port'] = int(cfg.get('port', 80))

    tmp_manifest = manifest_path() + '.part'
    with open(tmp_manifest, 'w') as mf:
        json.dump(data, mf, indent=2)
    os.replace(tmp_manifest, manifest_path())

    # Say plainly whether triggering will actually do anything: esp32FOTA
    # flashes only when the manifest version is strictly greater.
    will_update = device_fw == 0 or data['version'] > device_fw
    return jsonify({'ok': True, 'version': data['version'],
                    'version_source': version_source,
                    'device_version': device_fw or None,
                    'will_update': will_update,
                    'host': data['host'], 'port': data['port']})


@firmware_bp.route('/firmware/trigger', methods=['POST'])
def trigger():
    current_app.extensions['mqtt'].publish('irrigation/cmd/ota_update', 'trigger')
    return jsonify({'ok': True})
