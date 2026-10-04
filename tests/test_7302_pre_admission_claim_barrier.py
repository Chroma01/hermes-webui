"""A stream that was registered but whose worker has not been admitted yet must not
be reaped as an orphan, however old its pending turn is.

Re-gate regression for PR #7302 (review 2026-10-02T08:59:35Z, finding 5 -- "CORE"):

``_active_stream_blocks_chat_start()`` keeps a registered stream only while
``ACTIVE_RUNS`` holds a live-worker row or ``_pending_turn_in_registration_window()``
is true -- and that window requires a fresh ``pending_started_at``
(``_REPAIR_STALE_PENDING_GRACE_SECONDS``, 30 s). Both launch sites register the stream
and schedule the worker BEFORE the worker admits itself, and the regeneration site
holds that worker at ``release_worker.wait()`` while ``s.save()`` runs. A save slower
than the grace window -- or a turn whose pending timestamp was persisted earlier --
therefore leaves a stream that is genuinely launching, with no worker row and a stale
pending timestamp: the orphan check clears it and the first worker exits without
running its turn. master never clears a registered stream on age alone, so this is a
regression introduced by the orphan cleanup in this branch.

The fix publishes a launch-phase ownership claim atomically with the ``STREAMS``
registration and keeps it until worker admission, so the orphan check must keep the
stream while that claim exists, whatever the pending age.

The two tests below are the two rows of the maintainer's re-gate table: 5 s of pending
age is inside the registration window and must block; 35 s of pending age is past it
and must STILL block while the launch phase is in flight.

Grep for the assertions, not for source strings: nothing here reads the source text.
"""
import time

import pytest

import api.config as config
import api.routes as routes
from api.models import _REPAIR_STALE_PENDING_GRACE_SECONDS

SESSION_ID = "launch-session"
STREAM_ID = "launch-stream"


class _LaunchSession:
    """The attributes the registration window and the orphan check read."""

    def __init__(self, *, pending_age_seconds):
        self.session_id = SESSION_ID
        self.active_stream_id = STREAM_ID
        self.pending_user_message = "hello"
        self.pending_started_at = time.time() - pending_age_seconds


def _reset_registries():
    config.STREAMS.clear()
    config.ACTIVE_RUNS.clear()
    config.CANCEL_FLAGS.clear()
    config.STREAM_SESSION_OWNERS.clear()
    config.PRE_ADMISSION_CLAIMS.clear()


@pytest.fixture(autouse=True)
def _isolated_registries():
    _reset_registries()
    yield
    _reset_registries()


def _simulate_launch_registration(session):
    """What both launch sites do before scheduling their worker.

    A channel is created, the stream is registered with its owner, and the
    launch-phase ownership claim is published -- all in the same ``STREAMS_LOCK``
    critical section the fix uses. The worker has been scheduled but has NOT
    admitted itself yet, so there is deliberately no ``ACTIVE_RUNS`` row.
    """
    with config.STREAMS_LOCK:
        config.STREAMS[STREAM_ID] = config.create_stream_channel()
        config.publish_pre_admission_claim(STREAM_ID, streams_lock_held=True)
    config.register_stream_owner(STREAM_ID, session.session_id)


def test_fresh_pending_launch_blocks_a_duplicate_start():
    """Re-gate table, row 5 s: still inside the grace window -> blocked."""
    session = _LaunchSession(pending_age_seconds=5)
    _simulate_launch_registration(session)

    assert routes._active_stream_blocks_chat_start(session, STREAM_ID) is True
    assert STREAM_ID in config.STREAMS


def test_stale_pending_launch_is_not_orphaned_before_admission():
    """Re-gate table, row 31 s: past the grace window, launch phase still in flight.

    This is the regression. master keeps the stream here; this branch reaps it.
    """
    session = _LaunchSession(
        pending_age_seconds=_REPAIR_STALE_PENDING_GRACE_SECONDS + 5
    )
    _simulate_launch_registration(session)

    blocked = routes._active_stream_blocks_chat_start(session, STREAM_ID)

    assert blocked is True, (
        "chat/start was admitted over a stream that is still in its launch phase: "
        "the first worker would exit without running its turn"
    )
    assert STREAM_ID in config.STREAMS, (
        "the orphan check cleared a stream whose launch phase was still in flight"
    )
    assert config.STREAM_SESSION_OWNERS.get(STREAM_ID) == SESSION_ID, (
        "the orphan check released the owner of a stream that was still launching"
    )


def test_admission_retires_the_claim_and_a_later_orphan_is_still_reaped():
    """The claim must not survive worker admission, or a dead stream would lock out.

    Once the worker is admitted, ownership lives in ``ACTIVE_RUNS``; the claim is
    retired by the admission path. A stream that then loses both its worker row
    and every launch claim is a genuine orphan again and must be reaped -- the
    claim must not resurrect the 409 lockout the orphan cleanup was added to fix.
    """
    session = _LaunchSession(
        pending_age_seconds=_REPAIR_STALE_PENDING_GRACE_SECONDS + 5
    )
    _simulate_launch_registration(session)

    assert (
        config.retire_pre_admission_claim_if_owned(STREAM_ID) is True
    ), "the launch-phase claim was not retired at admission"

    assert routes._active_stream_blocks_chat_start(session, STREAM_ID) is False, (
        "a stream with no worker row and no launch claim was kept: the orphan "
        "cleanup is dead and every later chat/start would 409"
    )
    assert STREAM_ID not in config.STREAMS


def test_retiring_a_claim_by_identity_never_touches_a_successor():
    """A stale retire must not clear the claim a successor published."""
    successor_token = config.publish_pre_admission_claim(STREAM_ID)

    assert (
        config.retire_pre_admission_claim_if_owned(
            STREAM_ID, claim_token="a-superseded-token"
        )
        is False
    )
    assert config.PRE_ADMISSION_CLAIMS.get(STREAM_ID) == successor_token, (
        "retiring a superseded token cleared the successor's launch claim"
    )

    assert (
        config.retire_pre_admission_claim_if_owned(
            STREAM_ID, claim_token=successor_token
        )
        is True
    )
    assert STREAM_ID not in config.PRE_ADMISSION_CLAIMS


def test_the_canonical_teardown_retires_the_claim():
    """No leak: the single teardown entry point retires the claim with the stream."""
    config.publish_pre_admission_claim(STREAM_ID)
    assert STREAM_ID in config.PRE_ADMISSION_CLAIMS

    config.release_stream_owned_registries(STREAM_ID, session_id=SESSION_ID)

    assert STREAM_ID not in config.PRE_ADMISSION_CLAIMS, (
        "a claim outlived the canonical teardown of its stream"
    )
