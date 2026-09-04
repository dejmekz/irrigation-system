import json
import os
import yaml
from flask import Flask
from flask_socketio import SocketIO
from dotenv import load_dotenv

socketio = SocketIO()


def create_app(config_path: str = 'config.yaml') -> Flask:
    load_dotenv()

    app: Flask = Flask(
        __name__,
        template_folder='../templates',
        static_folder='../static',
    )

    with open(config_path) as f:
        config = yaml.safe_load(f)

    mqtt_cfg = config['mqtt']
    mqtt_cfg['username'] = os.getenv('MQTT_USER', mqtt_cfg.get('username', ''))
    mqtt_cfg['password'] = os.getenv('MQTT_PASS', mqtt_cfg.get('password', ''))

    fw_cfg = config.get('firmware', {}) or {}
    fw_cfg['dir'] = os.getenv('FIRMWARE_DIR', fw_cfg.get('dir', ''))
    fw_cfg['host'] = os.getenv('FIRMWARE_HOST', fw_cfg.get('host', 'raspi4server.local'))
    fw_cfg['port'] = int(os.getenv('FIRMWARE_PORT', fw_cfg.get('port', 80)))

    app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', config['web']['secret_key'])
    app.config['MQTT_CFG'] = mqtt_cfg
    app.config['SYS_CFG'] = config['system']
    app.config['WEB_CFG'] = config['web']
    app.config['FW_CFG'] = fw_cfg

    socketio.init_app(app, async_mode='threading', cors_allowed_origins='*')

    app.jinja_env.filters['fromjson'] = json.loads

    from .database import init_db
    init_db()

    from .mqtt_client import MQTTClient
    mqtt_client: MQTTClient = MQTTClient(config['mqtt'], socketio)
    mqtt_client.connect()
    app.extensions['mqtt'] = mqtt_client

    from .scheduler import IrrigationScheduler
    scheduler: IrrigationScheduler = IrrigationScheduler(
        mqtt_client, socketio,
        max_script_duration=config['system'].get('max_script_duration', 7200),
    )
    scheduler.start()
    scheduler.load_schedules()
    app.extensions['scheduler'] = scheduler

    mqtt_client.on_esp32_online = scheduler.resync_to_esp32

    from .routes import main_bp
    app.register_blueprint(main_bp)

    from .firmware import firmware_bp
    app.register_blueprint(firmware_bp)

    return app
