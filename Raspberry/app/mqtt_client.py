import copy
import json
import threading
import paho.mqtt.client as mqtt_lib
from .database import log_message
from flask_socketio import SocketIO
from typing import Any, Callable

TOPIC_BASE = 'irrigation'

# Commands must not be fire-and-forget: a dropped valve OFF leaves water running
# with nothing to retry it, and a dropped stop_all is an emergency button that
# silently did nothing. The ESP32 subscribes at QoS 1 to match — the broker
# delivers at min(publish QoS, subscription QoS).
QOS_COMMAND = 1


class MQTTClient:
    def __init__(self, config : dict[str, str], socketio : SocketIO):
        self.config = config
        self.socketio = socketio
        self.connected = False
        self._state_lock = threading.Lock()          # Fix #11: protect state dict
        # state[box_str] = {valves: {valve_str: 'ON'/'OFF'}, pump: 'ON'/'OFF'}
        self.state : dict[str, Any] = {'pump': 'OFF'}

        # Arbitrary external topics (e.g. an openHAB-bridged smart plug) that
        # schedules can gate on. topic -> last known payload.
        self._gate_topics: set[str] = set()
        self.gate_state: dict[str, str] = {}

        self._client = mqtt_lib.Client(
            client_id=config.get('client_id', 'irrigation_pi'),
            protocol=mqtt_lib.MQTTv311,
            callback_api_version=mqtt_lib.CallbackAPIVersion.VERSION1,
        )
        if config.get('username'):
            self._client.username_pw_set(
                config['username'], config.get('password', '')
            )

        self._first_connect = True
        # None until the first irrigation/status message. The ESP32 publishes
        # 'online' retained on connect and registers 'offline' as its LWT, so the
        # broker always holds a value — None means we have not subscribed long
        # enough to have received it, which is not the same as "offline".
        self._esp32_online: bool | None = None
        self._esp32_online_timer: threading.Timer | None = None
        self.on_esp32_online: Callable[[], None] | None = None

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

    def publish(self, topic, payload, qos: int = QOS_COMMAND) -> bool:
        """Publish and report whether the message actually left the client.
        A failed publish is logged as 'err' rather than 'out' so the message log
        never claims a command was sent when it wasn't."""
        info = self._client.publish(topic, payload, qos=qos)
        ok = info.rc == mqtt_lib.MQTT_ERR_SUCCESS
        if ok:
            logged, direction = payload, 'out'
        else:
            logged, direction = f'{payload}  [publish failed rc={info.rc}]', 'err'
            print(f'MQTT publish failed rc={info.rc}: {topic} = {payload!r}')
        log_message(topic, logged, direction)
        self.socketio.emit('log_entry', {
            'topic': topic, 'payload': logged, 'direction': direction,
        })
        return ok

    def set_valve(self, box, valve, on: bool) -> bool:
        return self.publish(
            f'{TOPIC_BASE}/box/{box}/valve/{valve}/set',
            'ON' if on else 'OFF',
        )

    def set_pump(self, on: bool) -> bool:
        return self.publish(
            f'{TOPIC_BASE}/pump/set',
            'ON' if on else 'OFF',
        )

    def all_off(self) -> bool:
        return self.publish(f'{TOPIC_BASE}/cmd/stop_all', '')

    def get_state(self):
        with self._state_lock:                       # Fix #11: return snapshot, not live ref
            return copy.deepcopy(self.state)

    def subscribe_gate(self, topic: str):
        """Track an arbitrary external topic (e.g. a smart-plug state) that a
        schedule can gate on. Idempotent; safe to call before connect()."""
        with self._state_lock:
            already = topic in self._gate_topics
            self._gate_topics.add(topic)
        if not already and self.connected:
            self._client.subscribe(topic)

    def get_gate_state(self, topic: str) -> str | None:
        with self._state_lock:
            return self.gate_state.get(topic)

    def get_gate_states(self) -> dict[str, str]:
        with self._state_lock:
            return dict(self.gate_state)

    def esp32_status(self) -> bool | None:
        """Whether the ESP32 is reachable, per its retained irrigation/status
        message and LWT. None means no status has been received yet — callers
        must not read that as "online"."""
        with self._state_lock:
            return self._esp32_online

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
            client.subscribe(f'{TOPIC_BASE}/hw_status')
            with self._state_lock:
                gate_topics = set(self._gate_topics)
            for topic in gate_topics:
                client.subscribe(topic)
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

        with self._state_lock:
            is_gate = topic in self._gate_topics
            if is_gate:
                self.gate_state[topic] = payload
        if is_gate:
            self.socketio.emit('gate_update', {'topic': topic, 'payload': payload})
            return

        # irrigation/pump/state                     (len=3)
        # irrigation/box/{box}/valve/{valve}/state  (len=6)
        # irrigation/status                         (len=2)
        parts = topic.split('/')

        if (len(parts) == 2
                and parts[0] == TOPIC_BASE
                and parts[1] == 'status'):
            if payload == 'offline':
                with self._state_lock:
                    self._esp32_online = False
                    self.state = {'pump': 'OFF'}
                    snapshot = copy.deepcopy(self.state)
                self.socketio.emit('esp32_status', {'online': False})
                self.socketio.emit('state_update', snapshot)
            elif payload == 'online':
                with self._state_lock:
                    self._esp32_online = True
                self.socketio.emit('esp32_status', {'online': True})
                if self.on_esp32_online:
                    # Cancel any pending timer before starting a new one
                    if self._esp32_online_timer is not None:
                        self._esp32_online_timer.cancel()
                    # 1.5 s delay lets the ESP32 finish subscribing before we resend state
                    self._esp32_online_timer = threading.Timer(1.5, self.on_esp32_online)
                    self._esp32_online_timer.start()
            return

        if (len(parts) == 3
                and parts[0] == TOPIC_BASE
                and parts[1] == 'pump'
                and parts[2] == 'state'):
            with self._state_lock:
                self.state['pump'] = payload
                snapshot = copy.deepcopy(self.state)
            self.socketio.emit('state_update', snapshot)

        elif (len(parts) == 2
                and parts[0] == TOPIC_BASE
                and parts[1] == 'hw_status'):
            # Ignore retained hw_status delivered after ESP32 goes offline
            with self._state_lock:
                online = self._esp32_online
            if not online:
                return
            try:
                hw = json.loads(payload)
            except Exception:
                hw = {}
            with self._state_lock:
                self.state['hw'] = hw
                snapshot = copy.deepcopy(self.state)
            self.socketio.emit('state_update', snapshot)

        elif (len(parts) == 2
                and parts[0] == TOPIC_BASE
                and parts[1] == 'heartbeat'):
            try:
                hb = json.loads(payload)
            except Exception:
                hb = {}
            with self._state_lock:
                if 'fw' in hb:
                    self.state['fw'] = hb['fw']
                if 'hw_ok' in hb:
                    self.state['hw_ok'] = hb['hw_ok']
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
