"""A reattached Gateway run must claim its ownership BEFORE the worker is scheduled.

Re-gate regression for PR #7302 (review 2026-10-01, finding 2 -- "CORE"):

``_resume_gateway_run_for_session()`` registered the restored run in ``STREAMS`` and
scheduled the worker, and only THEN did the worker publish its ``ACTIVE_RUNS`` row.
A run restored after a WebUI restart carries the PREVIOUS process's
``session.pending_started_at``, so the fresh-pending guard that covers the ordinary
registration gap in ``_active_stream_blocks_chat_start`` cannot cover this one: the
orphan check saw a registered stream with no live worker and an old pending
timestamp, classified the reattach as an orphan, cleared the stream plus the
Gateway state, and admitted a SECOND start while the remote run kept going.

The fix publishes the ``ACTIVE_RUNS`` row (and the retained cancel signal) on the
same ``STREAMS_LOCK -> ACTIVE_RUNS_LOCK`` edge that creates the ``STREAMS`` entry,
before the thread is scheduled, and releases the claim if the thread fails to
launch. These tests pin the invariant and its observable consequence.

Grep for the assertions, not for source strings: nothing here reads the source text.
"""
import queue
import threading
import time

import pytest

import api.config as config
import api.gateway_chat as gateway_chat
import api.routes as routes

SESSION_ID = "reattach-session"
STREAM_ID = "reattach-stream"
RUN_ID = "gateway-run-1"


class _ProbeStop(Exception):
    """Unwinds the resume path right after the publication it is being probed for."""


class _FakeSession:
    """The attributes ``_resume_gateway_run_for_session`` reads."""

    def __init__(self, *, pending_started_at):
        self.session_id = SESSION_ID
        self.active_stream_id = STREAM_ID
        self.gateway_run = {
            "stream_id": STREAM_ID,
            "run_id": RUN_ID,
        }
        self.pending_user_message = "hello"
        self.model = "test-model"
        self.model_provider = "test-provider"
        self.workspace = "test-workspace"
        self.pending_attachments = []
        self.profile = None
        # A RESTARTED session: the timestamp belongs to the previous process.
        self.pending_started_at = pending_started_at


def _reset_registries():
    config.STREAMS.clear()
    config.ACTIVE_RUNS.clear()
    config.CANCEL_FLAGS.clear()
    config.STREAM_SESSION_OWNERS.clear()


@pytest.fixture(autouse=True)
def _isolated_registries(monkeypatch):
    _reset_registries()
    # Endpoint resolution touches profiles/config; irrelevant to the invariant.
    monkeypatch.setattr(
        gateway_chat, "_gateway_endpoint_for_profile", lambda profile: ("http://gw", "k")
    )
    yield
    _reset_registries()


class _RecordingThread:
    """Stands in for ``threading.Thread``: records the launch, never runs the body."""

    launched: list = []

    def __init__(self, target=None, args=(), kwargs=None, **rest):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}
        self.__class__.launched.append(self)

    def start(self):
        pass


def test_reattach_publishes_its_active_run_inside_the_streams_lock_edge(monkeypatch):
    """The ACTIVE_RUNS publication must happen while STREAMS_LOCK is held."""
    session = _FakeSession(pending_started_at=time.time() - 3600)
    observed = {}

    def probing_register_active_run(sid, **metadata):
        # A non-blocking acquire answers "did the caller already hold it?" with no timing.
        acquired = config.STREAMS_LOCK.acquire(blocking=False)
        observed["inside_streams_lock"] = not acquired
        observed["stream_id"] = sid
        observed["phase"] = metadata.get("phase")
        if acquired:
            config.STREAMS_LOCK.release()
        raise _ProbeStop

    monkeypatch.setattr(gateway_chat, "register_active_run", probing_register_active_run)
    monkeypatch.setattr(gateway_chat.threading, "Thread", _RecordingThread)

    with pytest.raises(_ProbeStop):
        gateway_chat._resume_gateway_run_for_session(session)

    assert observed.get("stream_id") == STREAM_ID, (
        "the reattach never reached the publication point, so the probe observed nothing"
    )
    assert observed["inside_streams_lock"] is True, (
        "the reattach published its active run AFTER releasing STREAMS_LOCK: a concurrent "
        "chat/start can classify it as an orphan in that gap and admit a second start"
    )
    assert observed["phase"] == "gateway-reattached"


def test_restarted_run_with_a_stale_pending_timestamp_is_not_orphaned(monkeypatch):
    """The observable consequence: the reattach survives the orphan check.

    The session's pending timestamp is an hour old (it belongs to the process that
    died), which alone is enough for ``_active_stream_blocks_chat_start`` to treat
    the stream as an orphan. The pre-scheduled ACTIVE_RUNS row is what keeps it live.
    """
    _RecordingThread.launched = []
    monkeypatch.setattr(gateway_chat.threading, "Thread", _RecordingThread)
    session = _FakeSession(pending_started_at=time.time() - 3600)

    assert gateway_chat._resume_gateway_run_for_session(session) is True

    assert STREAM_ID in config.STREAMS, "the reattach did not register its stream"
    assert STREAM_ID in config.ACTIVE_RUNS, (
        "the reattach did not claim ACTIVE_RUNS before scheduling the worker, so the "
        "orphan check has no liveness evidence for a restarted run"
    )
    assert len(_RecordingThread.launched) == 1, "the reattach worker was never scheduled"

    assert routes._active_stream_blocks_chat_start(session, STREAM_ID) is True, (
        "a reattached run whose pending timestamp predates the restart was classified as "
        "an orphan: chat/start would clear it and admit a second start"
    )
    assert STREAM_ID in config.STREAMS, "the orphan check cleared a live reattach"


def test_reattach_releases_its_claim_when_the_worker_cannot_be_scheduled(monkeypatch):
    """A failed launch must leave no ownership behind."""
    monkeypatch.setattr(gateway_chat, "_gateway_endpoint_for_profile", lambda profile: ("http://gw", "k"))
    session = _FakeSession(pending_started_at=time.time() - 3600)

    def exploding_thread(*args, **kwargs):
        raise RuntimeError("cannot start new thread")

    monkeypatch.setattr(gateway_chat.threading, "Thread", exploding_thread)

    assert gateway_chat._resume_gateway_run_for_session(session) is False

    assert STREAM_ID not in config.STREAMS, "a failed reattach left the stream registered"
    assert STREAM_ID not in config.ACTIVE_RUNS, (
        "a failed reattach left an ACTIVE_RUNS claim: the session would look busy forever"
    )
    assert config.stream_owner_session_id(STREAM_ID) is None, (
        "a failed reattach left the stream owner entry behind"
    )
    assert STREAM_ID not in config.CANCEL_FLAGS, "a failed reattach left its cancel flag behind"


def test_second_reattach_of_the_same_stream_is_refused(monkeypatch):
    """The registration guard still refuses a stream that is already registered."""
    monkeypatch.setattr(gateway_chat.threading, "Thread", _RecordingThread)
    session = _FakeSession(pending_started_at=time.time() - 3600)
    config.STREAMS[STREAM_ID] = queue.Queue()

    assert gateway_chat._resume_gateway_run_for_session(session) is False
    assert STREAM_ID not in config.ACTIVE_RUNS, (
        "the refused reattach published an active run it does not own"
    )
