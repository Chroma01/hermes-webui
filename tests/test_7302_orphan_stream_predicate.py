"""An orphaned stream is defined once and reclaimable by every reader.

Re-gate regression for PR #7302 (greptile, finding 2 -- "orphan streams still
block"):

A ``STREAMS`` entry whose worker died before running its teardown was never
reclaimed. The session-channel reaper only ever looked at ``SESSION_CHANNELS``,
so the entry lived for the life of the process, and two things were observable:
``_session_has_active_turn()`` kept reporting the session busy (so sibling
async-delegation completions were refused against a stream that has no worker),
and every row the stream owned was retained.

The fix gives every reader ONE definition of "orphan" --
``api.config.is_orphaned_stream()``: registered, no worker row in
``ACTIVE_RUNS``, no launch-phase claim, and no pending turn inside its
registration window. ``_session_has_active_turn()`` asks it instead of trusting
``STREAMS`` membership, and the real reaper reclaims orphans through the
canonical teardown.

The launch-phase claim is the part that must not be lost: a stream that is still
launching is NOT an orphan, or the sweep would reap it and reintroduce finding 5
through the back door.

Grep for the assertions, not for source strings: nothing here reads the source text.
"""
import time

import pytest

from api import background_process as bp
from api import config as config

SESSION_ID = "orphan-session"
STREAM_ID = "orphan-stream"


def _reset_registries():
    config.STREAMS.clear()
    config.ACTIVE_RUNS.clear()
    config.CANCEL_FLAGS.clear()
    config.STREAM_SESSION_OWNERS.clear()
    # Pre-fix heads have no claim API: the behavioural tests must still reach their
    # assertions there (those failures ARE the RED), so this stays optional.
    getattr(config, "PRE_ADMISSION_CLAIMS", {}).clear()


@pytest.fixture(autouse=True)
def _isolated_registries():
    _reset_registries()
    yield
    _reset_registries()


_REQUIRES_ORPHAN_HELPER = pytest.mark.skipif(
    not hasattr(config, "is_orphaned_stream"),
    reason="shared orphan predicate not present (pre-fix RED)",
)


def _register_worker_less_stream():
    """Register a stream with an owner and nothing else: the orphan shape."""
    with config.STREAMS_LOCK:
        config.STREAMS[STREAM_ID] = config.create_stream_channel()
    config.register_stream_owner(STREAM_ID, SESSION_ID)


def _drive_reaper_until(predicate, timeout: float = 3.0, interval: float = 0.02):
    """Run the REAL ``_reaper_loop`` in a thread until ``predicate()`` holds."""
    original = bp._REAPER_INTERVAL_SECS
    bp._REAPER_INTERVAL_SECS = interval
    started = bp.start_session_channel_reaper()
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return predicate()
    finally:
        bp.stop_session_channel_reaper()
        bp._REAPER_INTERVAL_SECS = original
        del started  # only recorded for symmetry with the start/stop pairing


def test_an_orphaned_stream_does_not_keep_the_session_busy():
    """A worker-less STREAMS entry is not a live turn for the busy pre-check.

    ``_session_has_active_turn()`` is the pre-check sibling completions run
    against. Trusting ``STREAMS`` membership made an orphaned stream keep the
    session busy forever, so those completions were refused against a stream with
    no worker at all.
    """
    _register_worker_less_stream()

    assert bp._session_has_active_turn(SESSION_ID) is False, (
        "an orphaned stream kept the session reporting busy: sibling completions "
        "are refused against a stream that has no worker"
    )

    # Control: a live worker row still counts as busy.
    config.register_active_run(
        STREAM_ID, session_id=SESSION_ID, started_at=time.time(), phase="starting"
    )
    assert bp._session_has_active_turn(SESSION_ID) is True, (
        "a live worker row must still make the session busy"
    )


def test_the_reaper_reclaims_an_orphaned_stream():
    """The reaper must reclaim it through the canonical teardown, not leave it."""
    _register_worker_less_stream()
    assert STREAM_ID in config.STREAMS

    collected = _drive_reaper_until(lambda: STREAM_ID not in config.STREAMS)

    assert collected, (
        "the reaper never reclaimed an orphaned stream: the entry and every row it "
        "owns are retained for the life of the process"
    )
    assert config.stream_owner_session_id(STREAM_ID) is None, (
        "the reclaimed orphan kept its owner entry in the registry"
    )


@_REQUIRES_ORPHAN_HELPER
def test_the_shared_predicate_covers_every_liveness_signal():
    """One definition, four signals: membership, claim, worker, pending window."""
    _register_worker_less_stream()
    assert config.is_orphaned_stream(STREAM_ID) is True, (
        "a registered stream with no worker, no claim and no pending turn is the "
        "orphan the predicate exists to name"
    )

    # A launch-phase claim keeps a launching stream alive (finding 5).
    token = config.publish_pre_admission_claim(STREAM_ID)
    assert config.is_orphaned_stream(STREAM_ID) is False, (
        "the predicate ignored the launch-phase claim: a stream that is still "
        "launching would be reaped"
    )
    config.retire_pre_admission_claim_if_owned(STREAM_ID, claim_token=token)

    # A live worker keeps it alive.
    config.register_active_run(
        STREAM_ID, session_id=SESSION_ID, started_at=time.time(), phase="starting"
    )
    assert config.is_orphaned_stream(STREAM_ID) is False, (
        "the predicate ignored the worker row: a live turn would be reaped"
    )
    config.unregister_active_run(STREAM_ID)

    # A pending turn inside its window keeps it alive.
    assert config.is_orphaned_stream(STREAM_ID, pending_turn_in_window=True) is False

    # An unregistered stream is never an orphan.
    config.release_stream_owned_registries(STREAM_ID)
    assert config.is_orphaned_stream(STREAM_ID) is False
