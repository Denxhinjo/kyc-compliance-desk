"""What has to be true before the worker claims its first job.

Two deploy-readiness properties, both of which were assumptions until now:

  * the sanctions list is loaded at boot, so no job ever blocks on loading it
  * the reaper's stale threshold exceeds the longest job that can legitimately
    run, so it cannot reclaim work that is still in progress
"""

from __future__ import annotations

import logging
import sys
import types

import pytest

import main
from config import STALE_SECONDS, SWEEP_BATCH_SIZE, VENDOR_TIMEOUT_SECONDS
from screening import sources


@pytest.fixture(autouse=True)
def clean_cache():
    """Boot behaviour is about a cold process, so start each test cold."""
    sources._cached = None
    sources._preloaded = False
    yield
    sources._cached = None
    sources._preloaded = False


# ---------------------------------------------------------------------------
# Preloading
# ---------------------------------------------------------------------------


def test_preload_populates_the_cache():
    index = sources.preload("synthetic")

    assert len(index) == 25
    assert sources._cached is index
    assert sources._preloaded is True


def test_no_job_can_trigger_a_cold_load_after_preload(monkeypatch):
    """The point of the whole change, asserted rather than reasoned about.

    Every loader is replaced with one that raises. After preload, anything that
    reaches a loader fails loudly — so a passing test means nothing went back to
    disk. This is what "confirm no code path can cold-load mid-job" looks like
    as a test rather than as a claim.
    """
    sources.preload("synthetic")

    def explode():
        raise AssertionError(
            "a loader ran after boot — a claimed job is blocking on disk I/O, "
            "and its load time counts against JOB_STALE_SECONDS"
        )

    monkeypatch.setattr(
        sources, "_LOADERS", {name: explode for name in sources._LOADERS}
    )

    # The call the screening handler makes on every single job.
    for _ in range(3):
        index = sources.load_index("synthetic")
        assert len(index) == 25


def test_a_cold_load_after_boot_is_logged_loudly(monkeypatch, caplog):
    """If it ever does happen, it must not be silent.

    Reached only by changing the source mid-process, which a worker cannot do —
    but "cannot" is what the queue said about double-processing before the
    reaper's timeout was checked.
    """
    sources.preload("synthetic")

    with caplog.at_level(logging.WARNING, logger="worker.screening.sources"):
        # Provoke the miss without needing the gitignored OFAC file: a source
        # the cache does not hold, backed by the synthetic loader.
        monkeypatch.setitem(sources._LOADERS, "pretend", sources.load_synthetic)
        sources.load_index("pretend")

    assert any(
        "cold-loading" in record.message for record in caplog.records
    ), "a post-boot load must warn — it means a job is blocking on it"


def test_the_screening_handler_screens_against_the_database(
    db, make_application, monkeypatch
):
    """The handler reads the list from Postgres, not from this process.

    Once the list moved into the database there is nothing left to preload —
    what has to be true is that a snapshot exists and is searchable. A worker
    that boots clean and then cannot screen would be worse than one that
    refuses to start.
    """
    import screening_handler
    from jobs import Job, enqueue_job

    # The test database carries the committed synthetic fixture; .env may point
    # a developer's machine at OFAC.
    monkeypatch.setattr(screening_handler, "SANCTIONS_SOURCE", "synthetic")
    application_id = make_application("screening", full_name="Ahmed Hassan")
    job_id = enqueue_job(db, "screening.run", {"application_id": application_id})
    with db.cursor() as cur:
        cur.execute("select id, job_type, payload from jobs where id = %s", (job_id,))
        row = cur.fetchone()

    screening_handler.run_screening(
        db, Job(id=row[0], job_type=row[1], payload=row[2], attempts=1, max_attempts=5)
    )

    with db.cursor() as cur:
        cur.execute(
            "select count(*) from screening_results where application_id = %s",
            (application_id,),
        )
        assert cur.fetchone()[0] > 0


# ---------------------------------------------------------------------------
# Timeouts
# ---------------------------------------------------------------------------


def test_the_stale_threshold_exceeds_the_longest_legitimate_job():
    """The arithmetic that keeps the reaper from stealing live work.

    This failed with the values this project shipped for six phases:
    SWEEP_BATCH_SIZE 50 x VENDOR_TIMEOUT_SECONDS 10 = 500s against a 300s
    threshold. Nothing broke, because the simulator answers instantly — the
    margin only disappears when the vendor is slow, which is exactly when the
    sweeper matters most.
    """
    worst_case = SWEEP_BATCH_SIZE * VENDOR_TIMEOUT_SECONDS

    assert worst_case < STALE_SECONDS, (
        f"a sweep can take {worst_case:.0f}s but jobs are presumed dead after "
        f"{STALE_SECONDS:.0f}s — the reaper would reclaim a running sweep"
    )
    assert STALE_SECONDS / worst_case >= 2, (
        f"only a {STALE_SECONDS / worst_case:.1f}x margin; a single slow vendor "
        "response should not be able to close it"
    )


def test_the_boot_check_complains_when_the_margin_is_gone(monkeypatch, caplog):
    """The check has to actually fire, or it is decoration.

    Guards against the values being changed on Heroku by someone who has not
    read config.py — which is the realistic way this regresses.
    """
    monkeypatch.setattr(main, "SWEEP_BATCH_SIZE", 50)
    monkeypatch.setattr(main, "VENDOR_TIMEOUT_SECONDS", 10.0)
    monkeypatch.setattr(main, "STALE_SECONDS", 300.0)

    with caplog.at_level(logging.ERROR, logger="worker"):
        main._check_timeouts()

    assert any("MISCONFIGURED" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# The OFAC fallback
# ---------------------------------------------------------------------------


def test_without_a_fallback_a_failed_load_stops_the_worker(monkeypatch):
    """The development default: a missing list is a mistake, not a mode.

    Starting anyway would mean claiming screening jobs and failing every one.
    """
    monkeypatch.setitem(sources._LOADERS, "broken", _raise_missing)

    with pytest.raises(FileNotFoundError):
        sources.preload("broken")

    assert sources._preloaded is False, (
        "a worker that did not load a list must not look preloaded"
    )


def test_the_fallback_keeps_the_worker_running(monkeypatch, caplog):
    """Production's trade: degraded screening beats no screening."""
    monkeypatch.setitem(sources._LOADERS, "broken", _raise_missing)

    with caplog.at_level(logging.ERROR, logger="worker.screening.sources"):
        index = sources.preload("broken", fallback="synthetic")

    assert index.source == "synthetic"
    assert sources._preloaded is True
    assert any("DEGRADED" in record.message for record in caplog.records), (
        "falling back to a 25-entry invented list is not an INFO-level event"
    )


def test_a_degraded_run_is_visible_in_the_data_not_only_the_log(db, monkeypatch):
    """The part that matters after the logs have rotated.

    An auditor asking "what was this case screened against?" six months later
    has `sanctions_snapshots`, not `heroku logs`. A fallback that were only
    logged would be untraceable by exactly the person who needs to trace it.
    """
    monkeypatch.setitem(sources._LOADERS, "broken", _raise_missing)
    index = sources.preload("broken", fallback="synthetic")

    snapshot_id = sources.ensure_snapshot(db, index)
    with db.cursor() as cur:
        cur.execute(
            "select source from sanctions_snapshots where id = %s", (snapshot_id,)
        )
        assert cur.fetchone()[0] == "synthetic", (
            "the snapshot must name what was actually screened against"
        )


def test_the_fallback_does_not_hide_a_missing_fixture(monkeypatch):
    """If the fallback itself is broken, that has to surface."""
    monkeypatch.setitem(sources._LOADERS, "broken", _raise_missing)
    monkeypatch.setitem(sources._LOADERS, "synthetic", _raise_missing)

    with pytest.raises(FileNotFoundError):
        sources.preload("broken", fallback="synthetic")


def test_only_ofac_is_ever_downloaded(monkeypatch):
    """The synthetic fixture ships; OpenSanctions is a licensing decision.

    A boot that quietly fetched CC-BY-NC data on someone's behalf would be
    exactly the thing scripts/download_ofac.py exists to avoid.
    """
    # Point OFAC_PATH somewhere that does not exist, so "already present" cannot
    # be the reason nothing is fetched.
    monkeypatch.setattr(sources, "OFAC_PATH", sources.DATA_DIR / "does_not_exist.json")

    def explode():
        raise AssertionError("boot tried to download a list that is not OFAC")

    monkeypatch.setitem(
        sys.modules, "scripts.download_ofac", types.SimpleNamespace(main=explode)
    )

    for source in ("synthetic", "opensanctions"):
        sources.ensure_available(source)

    # And the control: OFAC with a missing file DOES reach the downloader.
    with pytest.raises(AssertionError, match="not OFAC"):
        sources.ensure_available("ofac")


def _raise_missing():
    raise FileNotFoundError("data/ofac_sdn.json is missing")
