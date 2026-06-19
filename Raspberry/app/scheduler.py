import json
from flask_socketio import SocketIO
import time
import threading
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.mqtt_client import MQTTClient
from . import database


class IrrigationScheduler:
    def __init__(self, mqtt_client: MQTTClient, socketio: SocketIO, max_script_duration: int = 7200):
        self.mqtt = mqtt_client
        self.socketio = socketio
        self._scheduler = BackgroundScheduler(daemon=True)
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._running = None
        self._max_duration = max_script_duration

    def start(self):
        self._scheduler.start()

    def load_schedules(self):
        for sched in database.get_schedules():
            if sched['enabled']:
                self._register(sched)

    def _register(self, sched):
        parts = sched['cron'].split()
        if len(parts) != 5:
            return
        minute, hour, day, month, dow = parts
        try:                                         # Fix #7: catch invalid cron fields
            self._scheduler.add_job(
                self.run_script,
                CronTrigger(minute=minute, hour=hour, day=day,
                            month=month, day_of_week=dow),
                args=[sched['script_id']],
                id=f"sched_{sched['id']}",
                replace_existing=True,
            )
        except Exception as exc:
            print(f"Invalid cron for schedule {sched['id']} ({sched['cron']}): {exc}")

    def reload_schedule(self, sched_id):
        try:
            self._scheduler.remove_job(f"sched_{sched_id}")
        except Exception:
            pass
        for sched in database.get_schedules():
            if sched['id'] == sched_id and sched['enabled']:
                self._register(sched)
                break

    def remove_schedule(self, sched_id):
        try:
            self._scheduler.remove_job(f"sched_{sched_id}")
        except Exception:
            pass

    def run_script(self, script_id: int):
        script = database.get_script(script_id)
        if not script:
            return
        steps = json.loads(script['steps'])
        pump_box = script.get('pump_box')
        pump_delay = int(script.get('pump_delay') or 0)

        with self._lock:
            if self._running is not None:            # Fix #8: falsy "" would bypass old guard
                return
            self._stop_event.clear()
            self._running = script['name']

        self.socketio.emit('script_status', {
            'running': script['name'], 'step': 0, 'total': len(steps),
        })

        def execute():
            pump_used = bool(pump_box)               # Fix #2: track all pump start paths
            watchdog = threading.Timer(self._max_duration, self._stop_event.set)
            watchdog.daemon = True
            watchdog.start()
            try:
                if pump_box and not self._stop_event.is_set():
                    self.mqtt.set_pump(True)
                    for _ in range(pump_delay):
                        if self._stop_event.is_set():
                            break
                        time.sleep(1)

                for i, step in enumerate(steps):
                    if self._stop_event.is_set():
                        break

                    action = step.get('action')
                    box = step.get('box', 1)
                    valve = step.get('valve', 1)
                    duration = int(step.get('duration') or 0)   # Fix #5: null → 0

                    self.socketio.emit('script_status', {
                        'running': script['name'],
                        'step': i + 1,
                        'total': len(steps),
                        'action': action,
                    })

                    if action == 'parallel_group':
                        for sub in step.get('actions', []):
                            sub_action = sub.get('action')
                            sub_box = sub.get('box', 1)
                            sub_valve = sub.get('valve', 1)
                            if sub_action == 'valve_on':
                                self.mqtt.set_valve(sub_box, sub_valve, True)
                            elif sub_action == 'valve_off':
                                self.mqtt.set_valve(sub_box, sub_valve, False)
                            elif sub_action == 'pump_on':
                                self.mqtt.set_pump(True)
                                pump_used = True
                            elif sub_action == 'pump_off':
                                self.mqtt.set_pump(False)
                    elif action == 'valve_on':
                        self.mqtt.set_valve(box, valve, True)
                    elif action == 'valve_off':
                        self.mqtt.set_valve(box, valve, False)
                    elif action == 'pump_on':
                        self.mqtt.set_pump(True)
                        pump_used = True
                    elif action == 'pump_off':
                        self.mqtt.set_pump(False)

                    if duration > 0:
                        for _ in range(duration):
                            if self._stop_event.is_set():
                                break
                            time.sleep(1)

                    # Auto-close after duration
                    if action == 'valve_on':
                        self.mqtt.set_valve(box, valve, False)
                    elif action == 'parallel_group':
                        for sub in step.get('actions', []):
                            sub_action = sub.get('action')
                            if sub_action == 'valve_on':
                                self.mqtt.set_valve(sub.get('box', 1), sub.get('valve', 1), False)
                            elif sub_action == 'pump_on':   # Fix #1: close pump started in group
                                self.mqtt.set_pump(False)
            finally:
                watchdog.cancel()
                if pump_used:                        # Fix #2: stop pump however it was started
                    self.mqtt.set_pump(False)
                with self._lock:
                    self._running = None
                self.socketio.emit('script_status', {
                    'running': None, 'step': 0, 'total': 0,
                })

        threading.Thread(target=execute, daemon=True).start()

    def stop_script(self):
        with self._lock:                             # Fix #4: only arm stop if a script is running
            if self._running is not None:
                self._stop_event.set()

    def get_status(self):
        with self._lock:                             # Fix #13: read under lock
            return {'running': self._running}
