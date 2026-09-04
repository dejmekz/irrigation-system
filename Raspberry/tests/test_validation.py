"""Script step validation. Two jobs: reject steps the scheduler cannot execute,
and reject a script that would hold a valve past the firmware's cap."""
import pytest

MAX_ON = 3600


@pytest.fixture
def validate(client):
    """_validate_steps needs no app context, but importing routes does."""
    from app.routes import _validate_steps

    def call(steps, max_on=MAX_ON, boxes=4, valves=3):
        return _validate_steps(steps, max_on, boxes, valves)

    return call


def valve_on(box=1, valve=1, duration=0):
    return {'action': 'valve_on', 'box': box, 'valve': valve, 'duration': duration}


# --- valve open time vs VALVE_MAX_ON_MS ---

def test_short_step_is_accepted(validate):
    assert validate([valve_on(duration=600)]) is None


def test_step_over_the_cap_is_rejected(validate):
    assert 'held open' in validate([valve_on(duration=5400)])


def test_step_exactly_at_the_cap_is_rejected(validate):
    """The firmware fires at >=, so the boundary is a race, not a valid value."""
    assert validate([valve_on(duration=MAX_ON)]) is not None


def test_untimed_open_plus_long_wait_is_rejected(validate):
    """The cap is on elapsed open time, not on one step's duration."""
    assert validate([
        valve_on(duration=0),
        {'action': 'wait', 'duration': 5400},
        {'action': 'valve_off', 'box': 1, 'valve': 1, 'duration': 0},
    ]) is not None


def test_untimed_open_plus_short_wait_is_accepted(validate):
    assert validate([
        valve_on(duration=0),
        {'action': 'wait', 'duration': 600},
        {'action': 'valve_off', 'box': 1, 'valve': 1, 'duration': 0},
    ]) is None


def test_reopening_the_same_valve_restarts_its_clock(validate):
    assert validate([valve_on(duration=3000), valve_on(duration=3000)]) is None


def test_long_parallel_group_is_rejected(validate):
    assert validate([{'action': 'parallel_group', 'duration': 5400, 'actions': [
        {'action': 'valve_on', 'box': 1, 'valve': 1},
        {'action': 'valve_on', 'box': 2, 'valve': 2},
    ]}]) is not None


def test_short_parallel_group_is_accepted(validate):
    assert validate([{'action': 'parallel_group', 'duration': 900, 'actions': [
        {'action': 'valve_on', 'box': 1, 'valve': 1},
        {'action': 'valve_on', 'box': 2, 'valve': 2},
    ]}]) is None


def test_cap_of_zero_disables_the_check(validate):
    assert validate([valve_on(duration=99999)], max_on=0) is None


# --- steps the scheduler cannot execute ---

def test_unknown_action_is_rejected(validate):
    """These used to fall through the scheduler's if/elif chain, sleep out their
    duration, and leave the run looking like a success."""
    err = validate([{'action': 'valve_of', 'box': 1, 'valve': 1, 'duration': 60}])
    assert 'unknown action' in err


def test_unknown_sub_action_is_rejected(validate):
    err = validate([{'action': 'parallel_group', 'duration': 60, 'actions': [
        {'action': 'sprinkle', 'box': 1, 'valve': 1},
    ]}])
    assert 'unknown action' in err


def test_parallel_group_needs_actions(validate):
    assert 'no actions' in validate([{'action': 'parallel_group', 'duration': 60}])


@pytest.mark.parametrize('box', [0, 5, -1, '1', True, None])
def test_box_out_of_range_is_rejected(validate, box):
    assert 'box must be' in validate([valve_on(box=box, duration=60)])


@pytest.mark.parametrize('valve', [0, 4, -1, '2', True])
def test_valve_out_of_range_is_rejected(validate, valve):
    assert 'valve must be' in validate([valve_on(valve=valve, duration=60)])


def test_ranges_follow_the_configured_dimensions(validate):
    assert validate([valve_on(box=5, duration=60)], boxes=6, valves=3) is None
    assert validate([valve_on(box=5, duration=60)], boxes=4, valves=3) is not None


def test_negative_duration_is_rejected(validate):
    assert validate([valve_on(duration=-5)]) is not None


def test_non_object_step_is_rejected(validate):
    assert validate(['nope']) is not None


def test_pump_steps_need_no_box_or_valve(validate):
    assert validate([{'action': 'pump_on', 'duration': 60},
                     {'action': 'pump_off', 'duration': 0}]) is None


def test_the_scripts_deployed_on_the_pi_still_validate(validate):
    """Regression guard: validation must not reject what is already in use."""
    assert validate([
        {'action': 'parallel_group', 'duration': 1200, 'actions': [
            {'action': 'valve_on', 'box': 1, 'valve': 1},
            {'action': 'valve_on', 'box': 1, 'valve': 3}]},
        {'action': 'parallel_group', 'duration': 1200, 'actions': [
            {'action': 'valve_on', 'box': 1, 'valve': 2},
            {'action': 'valve_on', 'box': 2, 'valve': 3}]},
        {'action': 'parallel_group', 'duration': 600, 'actions': [
            {'action': 'valve_on', 'box': 2, 'valve': 1},
            {'action': 'valve_on', 'box': 3, 'valve': 2}]},
        {'action': 'valve_on', 'duration': 1200, 'box': 2, 'valve': 2},
        {'action': 'parallel_group', 'duration': 600, 'actions': [
            {'action': 'valve_on', 'box': 3, 'valve': 3},
            {'action': 'valve_on', 'box': 4, 'valve': 3}]},
    ]) is None
