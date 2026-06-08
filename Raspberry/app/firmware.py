import json
import os
from flask import Blueprint, send_from_directory, request, jsonify, current_app

firmware_bp = Blueprint('firmware', __name__)

FIRMWARE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'firmware')
MANIFEST_PATH = os.path.join(FIRMWARE_DIR, 'manifest.json')


@firmware_bp.route('/firmware/manifest.json')
def manifest():
    try:
        with open(MANIFEST_PATH) as f:
            return jsonify(json.load(f))
    except FileNotFoundError:
        return jsonify({'error': 'manifest not found'}), 404


@firmware_bp.route('/firmware/irrigation.bin')
def binary():
    return send_from_directory(FIRMWARE_DIR, 'irrigation.bin')


@firmware_bp.route('/firmware/upload', methods=['POST'])
def upload():
    if 'firmware' not in request.files:
        return jsonify({'error': 'missing field: firmware'}), 400
    f = request.files['firmware']
    os.makedirs(FIRMWARE_DIR, exist_ok=True)
    f.save(os.path.join(FIRMWARE_DIR, 'irrigation.bin'))

    try:
        with open(MANIFEST_PATH) as mf:
            data = json.load(mf)
    except FileNotFoundError:
        data = {'type': 'irrigation-esp32c3', 'version': 0,
                'host': 'raspi4server.local', 'port': 5000,
                'bin': '/firmware/irrigation.bin'}

    data['version'] += 1
    with open(MANIFEST_PATH, 'w') as mf:
        json.dump(data, mf, indent=2)

    return jsonify({'ok': True, 'version': data['version']})


@firmware_bp.route('/firmware/trigger', methods=['POST'])
def trigger():
    current_app.extensions['mqtt'].publish('irrigation/cmd/ota_update', 'trigger')
    return jsonify({'ok': True})
