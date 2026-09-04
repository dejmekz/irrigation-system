"""Helpers shared by the scheduler tests."""
import time

# Three seconds of sleep: V1 open 1 s, wait 1 s, V2 open 1 s. Short enough to
# keep the suite quick, long enough for the per-second controller sampling.
STEPS = [
    {'action': 'valve_on', 'box': 1, 'valve': 1, 'duration': 1},
    {'action': 'wait', 'duration': 1},
    {'action': 'valve_on', 'box': 2, 'valve': 3, 'duration': 1},
]


def wait_for_run(scheduler, timeout=20):
    """Block until the script thread has finished its cleanup."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if scheduler.get_status()['running'] is None:
            break
        time.sleep(0.05)
    time.sleep(0.3)
