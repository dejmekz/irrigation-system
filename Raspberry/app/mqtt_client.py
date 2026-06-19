import copy
import threading
import paho.mqtt.client as mqtt_lib
from .database import log_message
from flask_socketio import SocketIO
from typing import Any

TOPIC_BASE = 'irrigation'


class MQTTClient:
    def __init__(self, config : dict[str, str], socketio : SocketIO):
        self.config = config
        self.socketio = socketio
        self.connected = False
        self._state_lock = threading.Lock()          # Fix #11: protect state dict
        # state[box_str] = {valves: {valve_str: 'ON'/'OFF'}, pump: 'ON'/'OFF'}
        self.state : dict[str, Any] = {'pump': 'OFF'}

        self._client = mqtt_lib.Client(
            client_id=config.get('client_id', 'irrigation_pi'),
            protocol=mqtt_lib.MQTTv311,
        )
        if config.get('username'):
            self._client.username_pw_set(
                config['username'], config.get('password', '')
            )

        self._first_connect = True

        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect

    def connect(self):
        try:
            self._client.connect_async(
                self.config.get('host', 'localhost'),
                self.config.get('port', 1883),
                keepalive=60,
            )
            self._client.loop_start()
        except Exception as exc:
            print(f'MQTT connect error: {exc}')

    def publish(self, topic, payload):
        self._client.publish(topic, payload)
        log_message(topic, payload, 'out')
        self.socketio.emit('log_entry', {
            'topic': topic, 'payload': payload, 'direction': 'out',
        })

    def set_valve(self, box, valve, on: bool):
        self.publish(
            f'{TOPIC_BASE}/box/{box}/valve/{valve}/set',
            'ON' if on else 'OFF',
        )

    def set_pump(self, on: bool):
        self.publish(
            f'{TOPIC_BASE}/pump/set',
            'ON' if on else 'OFF',
        )

    def all_off(self):
        self.publish(f'{TOPIC_BASE}/cmd/stop_all', '')

    def get_state(self):
        with self._state_lock:                       # Fix #11: return snapshot, not live ref
            return copy.deepcopy(self.state)

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.connected = True
            if self._first_connect:
                self._first_connect = False
                self.all_off()  # clear any stale relay state left over from a previous run
            client.subscribe(f'{TOPIC_BASE}/pump/state')
            client.subscribe(f'{TOPIC_BASE}/box/+/valve/+/state')
            client.subscribe(f'{TOPIC_BASE}/status')
            client.subscribe(f'{TOPIC_BASE}/heartbeat')
            self.socketio.emit('mqtt_status', {'connected': True})
            print('MQTT connected')
        else:
            print(f'MQTT connect failed rc={rc}')

    def _on_message(self, client, userdata, msg):
        topic = msg.topic
        payload = msg.payload.decode('utf-8', errors='replace')

        log_message(topic, payload, 'in')
        self.socketio.emit('log_entry', {
            'topic': topic, 'payload': payload, 'direction': 'in',
        })

        # irrigation/pump/state                     (len=3)
        # irrigation/box/{box}/valve/{valve}/state  (len=6)
        # irrigation/status                         (len=2)
        parts = topic.split('/')

        if (len(parts) == 2
                and parts[0] == TOPIC_BASE
                and parts[1] == 'status'):
            if payload == 'offline':                 # Fix #15: clear stale retained valve state
                with self._state_lock:
                    self.state = {'pump': 'OFF'}
                    snapshot = copy.deepcopy(self.state)
                self.socketio.emit('state_update', snapshot)
            return

        if (len(parts) == 3
                and parts[0] == TOPIC_BASE
                and parts[1] == 'pump'
                and parts[2] == 'state'):
            with self._state_lock:
                self.state['pump'] = payload
                snapshot = copy.deepcopy(self.state)
            self.socketio.emit('state_update', snapshot)

        elif len(parts) >= 4 and parts[0] == TOPIC_BASE and parts[1] == 'box':
            box = parts[2]
            snapshot = None
            with self._state_lock:                   # Fix #11: atomic check-then-set
                if box not in self.state:
                    self.state[box] = {'valves': {}}
                if (len(parts) == 6
                        and parts[3] == 'valve'
                        and parts[5] == 'state'):
                    self.state[box]['valves'][parts[4]] = payload
                    snapshot = copy.deepcopy(self.state)
            if snapshot is not None:
                self.socketio.emit('state_update', snapshot)

    def _on_disconnect(self, client, userdata, rc):
        self.connected = False
        self.socketio.emit('mqtt_status', {'connected': False})
        print(f'MQTT disconnected rc={rc}')
