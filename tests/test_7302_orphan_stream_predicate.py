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

_REQUIRES_RELEASE_HELPER = pytest.mark.skipif(
    not hasattr(config, "release_orphaned_stream_if_still_orphaned"),
    reason="decide-and-release helper not present (pre-fix RED)",
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


def test_a_published_stream_keeps_the_session_busy_for_the_pre_check():
    """A registered stream reads busy even before a worker row exists.

    ``_session_has_active_turn()`` is the pre-check sibling completions run
    against, and it only DEFERS: it never claims and never spends a delivery
    attempt. Upstream pins that contract
    (tests/test_async_delegation_webui_bridge.py::
    test_busy_predicate_covers_stream_publication_window_before_active_runs), so the
    published-stream window must read busy. The orphan itself is reclaimed by the
    channel reaper and cleared by the chat/start orphan path (see below).
    """
    _register_worker_less_stream()

    assert bp._session_has_active_turn(SESSION_ID) is True, (
        "the publication window must defer sibling completions instead of letting "
        "them claim against a stream that may still be live"
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


# ---------------------------------------------------------------------------
# Deciding and releasing must be ONE lock edge (re-gate finding A)
# ---------------------------------------------------------------------------


def test_a_stream_that_became_live_after_the_sweep_snapshot_survives():
    """The race greptile reported: a candidate becomes live before it is dropped.

    The sweep decided orphanhood under one ``STREAMS_LOCK`` acquisition and released
    under the next, so a worker that admitted its stream -- or a launch that
    published its claim -- in that gap had a LIVE stream deleted, ownership rows
    included, while the worker kept running. The release must re-validate inside the
    same critical section that drops the rows.
    """
    _register_worker_less_stream()

    release_if_orphan = getattr(
        config, "release_orphaned_stream_if_still_orphaned", None
    )
    if release_if_orphan is None:
        # Pre-fix shape: the caller's decision is taken here, and the rows go under
        # a SEPARATE acquisition further down.
        assert config.is_orphaned_stream(STREAM_ID) is True

    # The worker is admitted in that gap (a launch claim landing would be the same
    # shape, and is covered by the predicate test above).
    config.register_active_run(
        STREAM_ID, session_id=SESSION_ID, started_at=time.time(), phase="starting"
    )

    if release_if_orphan is not None:
        assert release_if_orphan(STREAM_ID) is False, (
            "the release re-validated inside its own acquisition, so a stream that "
            "became live after the caller's snapshot must not be released"
        )
    else:
        config.release_stream_owned_registries(STREAM_ID)

    assert STREAM_ID in config.STREAMS, (
        "a stream that became live after the sweep's snapshot was released: its "
        "worker keeps running with no stream and no ownership rows"
    )
    assert STREAM_ID in config.ACTIVE_RUNS, (
        "the live worker's own row was dropped along with the stream"
    )
    assert config.stream_owner_session_id(STREAM_ID) == SESSION_ID, (
        "the live stream lost its owner entry"
    )


@_REQUIRES_RELEASE_HELPER
def test_the_release_helper_refuses_a_stream_with_a_launch_claim():
    """A launching stream is not an orphan: the helper must leave it alone."""
    _register_worker_less_stream()
    config.publish_pre_admission_claim(STREAM_ID)

    assert config.release_orphaned_stream_if_still_orphaned(STREAM_ID) is False
    assert STREAM_ID in config.STREAMS
    assert STREAM_ID in config.PRE_ADMISSION_CLAIMS


@_REQUIRES_RELEASE_HELPER
def test_the_release_helper_refuses_a_stream_with_a_live_worker():
    """A running stream is not an orphan either."""
    _register_worker_less_stream()
    config.register_active_run(
        STREAM_ID, session_id=SESSION_ID, started_at=time.time(), phase="starting"
    )

    assert config.release_orphaned_stream_if_still_orphaned(STREAM_ID) is False
    assert STREAM_ID in config.STREAMS
    assert STREAM_ID in config.ACTIVE_RUNS


@_REQUIRES_RELEASE_HELPER
def test_the_release_helper_releases_a_true_orphan():
    """Contrast: nothing owns it, nothing is launching it -- it goes, and says so."""
    _register_worker_less_stream()

    assert config.release_orphaned_stream_if_still_orphaned(STREAM_ID) is True
    assert STREAM_ID not in config.STREAMS
    assert config.stream_owner_session_id(STREAM_ID) is None
    # Idempotent: a second call has nothing left to release.
    assert config.release_orphaned_stream_if_still_orphaned(STREAM_ID) is False
