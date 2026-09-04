import json
from flask_socketio import SocketIO
import time
import threading
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.events import EVENT_JOB_MISSED

from app.mqtt_client import MQTTClient
from . import database

# While a script runs, re-assert the state of everything it has opened at this
# interval. QoS 1 only covers the hop to the broker: this also recovers a valve
# whose command was lost while the ESP32 was rebooting, and it refreshes the
# firmware's PUMP_SAFETY_TIMEOUT_MS so steps longer than that are not cut short.
# It deliberately does not extend the firmware's VALVE_MAX_ON_MS cap, which is
# stamped on the OFF->ON edge only.
KEEPALIVE_INTERVAL_S = 300

# APScheduler's default misfire grace is 1 second: a brief stall or a clock nudge
# at the trigger instant silently drops the run with no trace anywhere. Five
# minutes survives that, while still being short enough that a run missed by a
# real outage is reported as missed rather than watering hours late.
MISFIRE_GRACE_S = 300

# How often to look for outputs left on that no script is managing.
UNMANAGED_CHECK_S = 30


class IrrigationScheduler:
    def __init__(self, mqtt_client: MQTTClient, socketio: SocketIO,
                 max_script_duration: int = 7200, offline_grace: int = 60,
                 manual_timeout: int = 1800):
        self.mqtt = mqtt_client
        self.socketio = socketio
        self._scheduler = BackgroundScheduler(
            daemon=True,
            job_defaults={'misfire_grace_time': MISFIRE_GRACE_S, 'coalesce': True},
        )
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._running = None
        self._open_valves: set[tuple[int, int]] = set()
        self._pump_open: bool = False
        self._last_keepalive: float = 0.0
        self._max_duration = max_script_duration
        self._offline_grace = offline_grace
        # Publishes that never left the MQTT client during the current run.
        # A run that watered nothing because the broker was down must not be
        # recorded as 'completed'.
        self._publish_failures = 0
        # Seconds of the current run during which the ESP32 was unreachable, and
        # whether any of that overlapped valves this script had opened.
        self._offline_seconds = 0
        self._offline_while_open = False
        self._manual_timeout = manual_timeout
        # Outputs reported ON that no running script opened -> when we first saw
        # them that way. Keyed (box, valve), or the string 'pump'.
        self._unmanaged_since: dict[object, float] = {}

    def start(self):
        self._scheduler.add_listener(self._on_job_missed, EVENT_JOB_MISSED)
        if self._manual_timeout > 0:
            self._scheduler.add_job(
                self._check_unmanaged, 'interval', seconds=UNMANAGED_CHECK_S,
                id='unmanaged_outputs', replace_existing=True,
            )
        self._scheduler.start()

    def _check_unmanaged(self):
        """Close anything left on that no script is managing — a valve opened
        from the dashboard and forgotten, or one a stopped script did not track.
        Until now the only bound on a mis-click was the firmware's 60 minute
        VALVE_MAX_ON_MS, i.e. an hour of unattended watering."""
        state = self.mqtt.get_state()
        now = time.monotonic()
        with self._lock:
            script_valves = set(self._open_valves)
            script_pump = self._pump_open

        live: set[object] = set()
        for box_key, data in state.items():
            if not isinstance(data, dict) or not data.get('valves'):
                continue
            for valve_key, value in data['valves'].items():
                if value != 'ON':
                    continue
                try:
                    key = (int(box_key), int(valve_key))
                except (TypeError, ValueError):
                    continue
                if key not in script_valves:
                    live.add(key)
        if state.get('pump') == 'ON' and not script_pump:
            live.add('pump')

        # Forget anything that has since gone off, so its timer restarts clean.
        for key in list(self._unmanaged_since):
            if key not in live:
                del self._unmanaged_since[key]

        for key in sorted(live, key=str):
            first = self._unmanaged_since.setdefault(key, now)
            if now - first < self._manual_timeout:
                continue
            del self._unmanaged_since[key]
            if key == 'pump':
                self.mqtt.set_pump(False)
                what = 'pump'
            else:
                self.mqtt.set_valve(key[0], key[1], False)   # type: ignore[index]
                what = f'valve {key[0]}/{key[1]}'            # type: ignore[index]
            msg = (f'Closed {what}: left on for {self._manual_timeout}s with no '
                   f'script managing it')
            print(msg)
            database.log_message('system/manual_timeout', msg, 'sys')
            self.socketio.emit('log_entry', {
                'topic': 'system/manual_timeout', 'payload': msg, 'direction': 'sys',
            })

    def _on_job_missed(self, event):
        """A run APScheduler could not start within the grace period. Without
        this it disappears silently — nothing in the log, nothing in history."""
        sched_id = None
        job_id = str(getattr(event, 'job_id', ''))
        if job_id.startswith('sched_'):
            try:
                sched_id = int(job_id.split('_', 1)[1])
            except ValueError:
                pass

        name, script_id, script_name = job_id or 'unknown', None, '?'
        for sched in database.get_schedules():
            if sched['id'] == sched_id:
                name = sched['name']
                script_id = sched['script_id']
                script_name = sched['script_name']
                break

        msg = (f'Schedule missed: "{name}" was due at '
               f'{getattr(event, "scheduled_run_time", "?")} but could not be '
               f'started within {MISFIRE_GRACE_S}s')
        print(msg)
        database.log_message('system/misfire', msg, 'sys')
        database.record_run(script_id, script_name, 'schedule', 'missed', msg,
                            schedule_id=sched_id, schedule_name=name)
        self.socketio.emit('log_entry', {
            'topic': 'system/misfire', 'payload': msg, 'direction': 'sys',
        })

    def load_schedules(self):
        for sched in database.get_schedules():
            # Subscribe regardless of enabled state so the gate badge and
            # last-known value are ready before a schedule gets turned on.
            if sched.get('gate_topic'):
                self.mqtt.subscribe_gate(sched['gate_topic'])
            if sched['enabled']:
                self._register(sched)

    def _register(self, sched):
        parts = sched['cron'].split()
        if len(parts) != 5:
            return
        minute, hour, day, month, dow = parts
        gate_topic = sched.get('gate_topic')
        if gate_topic:
            self.mqtt.subscribe_gate(gate_topic)
        try:                                         # Fix #7: catch invalid cron fields
            self._scheduler.add_job(
                self._run_scheduled,
                CronTrigger(minute=minute, hour=hour, day=day,
                            month=month, day_of_week=dow),
                args=[sched['script_id'], sched['id'], sched['name'],
                      gate_topic, sched.get('gate_payload') or 'ON'],
                id=f"sched_{sched['id']}",
                replace_existing=True,
            )
        except Exception as exc:
            print(f"Invalid cron for schedule {sched['id']} ({sched['cron']}): {exc}")

    def _run_scheduled(self, script_id: int, sched_id: int, sched_name: str,
                       gate_topic: str | None, gate_payload: str):
        """Cron entry point: skip the run if the schedule is gated on an
        external MQTT topic (e.g. a smart plug) that isn't in the expected state."""
        if gate_topic:
            current = self.mqtt.get_gate_state(gate_topic)
            if current != gate_payload:
                # Distinguish "no value ever seen on this topic" from a real
                # mismatch — the first usually means a missing retain flag
                # upstream, and looks identical in the log otherwise.
                seen = 'never received' if current is None else f'{current!r}'
                msg = (f'Schedule skipped: {gate_topic} = {seen} '
                       f'(expected {gate_payload!r})')
                database.log_message('system/gate', msg, 'sys')
                script = database.get_script(script_id)
                database.record_run(
                    script_id, script['name'] if script else f'script {script_id}',
                    'schedule', 'skipped', msg,
                    schedule_id=sched_id, schedule_name=sched_name)
                self.socketio.emit('log_entry', {
                    'topic': 'system/gate', 'payload': msg, 'direction': 'sys',
                })
                return
        self.run_script(script_id, trigger='schedule',
                        schedule_id=sched_id, schedule_name=sched_name)

    def reload_schedule(self, sched_id):
        try:
            self._scheduler.remove_job(f"sched_{sched_id}")
        except Exception:
            pass
        for sched in database.get_schedules():
            if sched['id'] == sched_id:
                if sched.get('gate_topic'):
                    self.mqtt.subscribe_gate(sched['gate_topic'])
                if sched['enabled']:
                    self._register(sched)
                break

    def remove_schedule(self, sched_id):
        try:
            self._scheduler.remove_job(f"sched_{sched_id}")
        except Exception:
            pass

    def _pub_valve(self, box: int, valve: int, on: bool):
        if not self.mqtt.set_valve(box, valve, on):
            with self._lock:
                self._publish_failures += 1

    def _pub_pump(self, on: bool):
        if not self.mqtt.set_pump(on):
            with self._lock:
                self._publish_failures += 1

    def _sample_controller(self):
        """Notice the controller going away mid-run. Checking only at the start
        is not enough: the broker keeps accepting publishes after the ESP32
        drops, so every command still reports success while nothing acts on it,
        and the run finishes looking clean.

        This deliberately only observes. A short outage is genuinely recovered —
        the firmware closes its own valves once WiFi has been gone for
        WIFI_ACTIVE_SAFETY_MS, and resync_to_esp32 re-asserts what the script
        had open when it reconnects — so aborting on any blip would fight that
        design and stop watering that would otherwise have resumed."""
        if self.mqtt.esp32_status() is True:
            return
        with self._lock:
            self._offline_seconds += 1
            first = self._offline_seconds == 1
            if self._open_valves or self._pump_open:
                self._offline_while_open = True
            name = self._running
        # Once per run: enough to timestamp the drop in the log without adding a
        # line every second for the whole outage.
        if first and name:
            msg = (f'Controller went offline during "{name}" — commands are '
                   f'reaching nothing until it returns')
            print(msg)
            database.log_message('system/offline', msg, 'sys')
            self.socketio.emit('log_entry', {
                'topic': 'system/offline', 'payload': msg, 'direction': 'sys',
            })

    def _sleep(self, seconds: int):
        """Sleep in 1 s ticks, returning early once a stop is requested, and
        re-asserting open valve/pump state every KEEPALIVE_INTERVAL_S."""
        for _ in range(max(0, seconds)):
            if self._stop_event.is_set():
                return
            time.sleep(1)
            self._sample_controller()
            self._keepalive()

    def _keepalive(self):
        now = time.monotonic()
        with self._lock:
            if now - self._last_keepalive < KEEPALIVE_INTERVAL_S:
                return
            self._last_keepalive = now
            open_valves = sorted(self._open_valves)
            pump_open = self._pump_open
        for box, valve in open_valves:
            self._pub_valve(box, valve, True)
        if pump_open:
            self._pub_pump(True)

    def run_script(self, script_id: int, trigger: str = 'manual',
                   schedule_id: int | None = None,
                   schedule_name: str | None = None) -> dict:
        """Start a script. Returns {'ok': True, ...} or, when the run is
        refused, {'ok': False, 'reason': ..., 'error': ...} so a caller can say
        what happened instead of reporting every request as accepted."""
        script = database.get_script(script_id)
        if not script:
            database.record_run(script_id, f'script {script_id}', trigger, 'error',
                                'script not found', schedule_id, schedule_name)
            return {'ok': False, 'reason': 'not_found', 'error': 'script not found'}
        # Nothing downstream ever checks whether the controller is reachable, so
        # without this a run against an offline ESP32 publishes into the void,
        # waters nothing, and is recorded as a clean 'completed'.
        online = self.mqtt.esp32_status()
        if online is not True:
            msg = ('Run aborted: controller is offline' if online is False else
                   'Run aborted: controller status unknown — no irrigation/status '
                   'message received yet')
            database.log_message('system/offline', msg, 'sys')
            database.record_run(script_id, script['name'], trigger, 'offline', msg,
                                schedule_id, schedule_name)
            self.socketio.emit('log_entry', {
                'topic': 'system/offline', 'payload': msg, 'direction': 'sys',
            })
            print(msg)
            return {'ok': False, 'reason': 'offline', 'error': msg}

        steps = json.loads(script['steps'])
        pump_box = script.get('pump_box')
        pump_delay = int(script.get('pump_delay') or 0)

        with self._lock:
            blocked_by = self._running                # Fix #8: falsy "" would bypass old guard
            if blocked_by is None:
                self._stop_event.clear()
                self._running = script['name']
                self._last_keepalive = time.monotonic()
                self._publish_failures = 0
                self._offline_seconds = 0
                self._offline_while_open = False
        if blocked_by is not None:
            # Recorded rather than silently dropped: "the 20:00 run never
            # happened because the 19:40 one was still going" is exactly the
            # question this history exists to answer. (DB write is outside the
            # lock deliberately.)
            msg = f'another script was already running: {blocked_by!r}'
            database.record_run(script_id, script['name'], trigger, 'blocked',
                                msg, schedule_id, schedule_name)
            return {'ok': False, 'reason': 'blocked', 'error': msg}

        run_id = database.start_run(script_id, script['name'], trigger,
                                    schedule_id, schedule_name)

        self.socketio.emit('script_status', {
            'running': script['name'], 'step': 0, 'total': len(steps),
        })

        def execute():
            pump_used = bool(pump_box)               # Fix #2: track all pump start paths
            outcome, detail = 'completed', None
            watchdog_fired = threading.Event()

            def _watchdog():
                watchdog_fired.set()
                self._stop_event.set()

            watchdog = threading.Timer(self._max_duration, _watchdog)
            watchdog.daemon = True
            watchdog.start()
            try:
                if pump_box and not self._stop_event.is_set():
                    self._pub_pump(True)
                    with self._lock:
                        self._pump_open = True
                    self._sleep(pump_delay)

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
                                self._pub_valve(sub_box, sub_valve, True)
                                with self._lock:
                                    self._open_valves.add((sub_box, sub_valve))
                            elif sub_action == 'valve_off':
                                self._pub_valve(sub_box, sub_valve, False)
                                with self._lock:
                                    self._open_valves.discard((sub_box, sub_valve))
                            elif sub_action == 'pump_on':
                                self._pub_pump(True)
                                pump_used = True
                                with self._lock:
                                    self._pump_open = True
                            elif sub_action == 'pump_off':
                                self._pub_pump(False)
                                with self._lock:
                                    self._pump_open = False
                    elif action == 'valve_on':
                        self._pub_valve(box, valve, True)
                        with self._lock:
                            self._open_valves.add((box, valve))
                    elif action == 'valve_off':
                        self._pub_valve(box, valve, False)
                        with self._lock:
                            self._open_valves.discard((box, valve))
                    elif action == 'pump_on':
                        self._pub_pump(True)
                        pump_used = True
                        with self._lock:
                            self._pump_open = True
                    elif action == 'pump_off':
                        self._pub_pump(False)
                        with self._lock:
                            self._pump_open = False

                    if duration > 0:
                        self._sleep(duration)

                        # Auto-close only for a TIMED step. A valve_on with no
                        # duration means "open and leave open", which is what
                        # makes the editor's separate Wait and Valve OFF steps
                        # usable — previously such a step opened the valve and
                        # shut it again in the same instant, silently doing
                        # nothing. Anything still open when the script ends is
                        # closed by the cleanup in the finally block.
                        if action == 'valve_on':
                            self._pub_valve(box, valve, False)
                            with self._lock:
                                self._open_valves.discard((box, valve))
                        elif action == 'pump_on':
                            # Was asymmetric with valve_on: a timed pump_on ran
                            # the pump for its duration and then left it going.
                            self._pub_pump(False)
                            with self._lock:
                                self._pump_open = False
                        elif action == 'parallel_group':
                            for sub in step.get('actions', []):
                                sub_action = sub.get('action')
                                if sub_action == 'valve_on':
                                    self._pub_valve(sub.get('box', 1), sub.get('valve', 1), False)
                                    with self._lock:
                                        self._open_valves.discard((sub.get('box', 1), sub.get('valve', 1)))
                                elif sub_action == 'pump_on':   # Fix #1: close pump started in group
                                    self._pub_pump(False)
                                    with self._lock:
                                        self._pump_open = False
            except Exception as exc:
                outcome = 'error'
                detail = f'{type(exc).__name__}: {exc}'
                print(f'Script {script["name"]!r} failed: {exc}')
            finally:
                watchdog.cancel()
                if pump_used:                        # Fix #2: stop pump however it was started
                    self._pub_pump(False)
                # Close whatever the script left open: an untimed valve_on stays
                # open by design, and a stop can interrupt anywhere. Pump first,
                # then valves, so it never runs against closed ones.
                with self._lock:
                    leftover = sorted(self._open_valves)
                for lb, lv in leftover:
                    self._pub_valve(lb, lv, False)
                with self._lock:
                    self._open_valves.clear()
                    self._pump_open = False
                    self._running = None
                    failures = self._publish_failures
                    offline_s = self._offline_seconds
                    offline_open = self._offline_while_open

                if outcome == 'completed' and self._stop_event.is_set():
                    outcome = 'stopped'
                    detail = (f'exceeded max_script_duration ({self._max_duration}s)'
                              if watchdog_fired.is_set() else 'stopped manually')
                # Everything below means some part of this run did not reach the
                # valves. Collect the reasons first, then decide the outcome once.
                notes: list[str] = []
                degrade = False
                if failures:
                    notes.append(f'{failures} MQTT command(s) never left the client — '
                                 f'the controller may not have received them')
                    degrade = True
                if offline_s > self._offline_grace:
                    where = ' while valves were open' if offline_open else ''
                    notes.append(f'controller was unreachable for {offline_s}s{where} — '
                                 f'commands sent during that time reached nothing')
                    degrade = True
                elif offline_s:
                    # Inside the grace window: worth recording, not worth failing.
                    notes.append(f'controller was briefly unreachable ({offline_s}s); '
                                 f'open valves were re-asserted on reconnect')
                if notes:
                    joined = '; '.join(notes)
                    if degrade and outcome == 'completed':
                        outcome, detail = 'error', joined
                    else:
                        detail = f'{detail}; {joined}' if detail else joined
                database.finish_run(run_id, outcome, detail)

                self.socketio.emit('script_status', {
                    'running': None, 'step': 0, 'total': 0,
                })

        threading.Thread(target=execute, daemon=True).start()
        return {'ok': True, 'running': script['name'], 'steps': len(steps)}

    def stop_script(self):
        with self._lock:                             # Fix #4: only arm stop if a script is running
            if self._running is not None:
                self._stop_event.set()

    def get_status(self):
        with self._lock:                             # Fix #13: read under lock
            return {'running': self._running}

    def resync_to_esp32(self):
        """Re-send active valve/pump state after an ESP32 restart mid-script."""
        with self._lock:
            if self._running is None:
                return
            open_valves = set(self._open_valves)
            pump_open = self._pump_open
            name = self._running

        msg = f'ESP32 reconnected during "{name}" — resyncing'
        database.log_message('system/resync', msg, 'sys')
        self.socketio.emit('log_entry', {
            'topic': 'system/resync', 'payload': msg, 'direction': 'sys',
        })

        for box, valve in sorted(open_valves):
            self._pub_valve(box, valve, True)
        if pump_open:
            self._pub_pump(True)
