import datetime
import json

import pytest

from ckan import model
from ckan.plugins import toolkit
from ckantoolkit.tests import helpers

from ckanext.harvest.harvesters.base import (
    HarvesterBase,
    DEFAULT_INCREMENTAL_OVERLAP_MINUTES,
    INCREMENTAL_SAFETY_MARGIN,
)
from ckanext.harvest.harvesters.ckanharvester import CKANHarvester
from ckanext.harvest.tests.factories import HarvestSourceObj, HarvestJobObj
from ckanext.harvest.tests.lib import run_harvest
from ckanext.harvest.tests.harvesters import mock_ckan
from ckanext.harvest import model as harvest_model

# Start the CKAN-alike server used by the integration test. ``serve`` is
# idempotent, so this is safe even if another test module started it first.
mock_ckan.serve()


class IncrementalHarvester(HarvesterBase):
    '''A minimal harvester that opts into resumable incremental windows.'''

    incremental_supported = True

    def info(self):
        return {
            'name': 'incremental',
            'title': 'Incremental',
            'description': 'Incremental harvester for tests',
        }


def _new_harvester(cls=IncrementalHarvester, config=None):
    '''Build a harvester instance bypassing ``SingletonPlugin.__new__``.

    ``HarvesterBase`` is a ``SingletonPlugin``, so subclassing it and calling
    the subclass directly can return an inherited base-class singleton once
    other tests have instantiated ``HarvesterBase``. Tests only need the
    behaviour, not the singleton, so allocate a plain instance.
    '''
    harvester = object.__new__(cls)
    harvester.config = config or {}
    return harvester


def _source(url):
    '''Create a harvest source with a backing dataset package.'''
    return HarvestSourceObj(url=url, source_type='ckan')


def _job(source, until=None, since=None, status='New'):
    job = HarvestJobObj(source=source)
    job.gather_finished = datetime.datetime.utcnow()
    job.incremental_since = since
    job.incremental_until = until
    job.status = status
    job.save()
    return job


def _object(job, guid, state):
    obj = harvest_model.HarvestObject(guid=guid, job=job,
                                      harvest_source_id=job.source.id,
                                      state=state, content='{}')
    obj.save()
    return obj


@pytest.mark.usefixtures('with_plugins', 'clean_db')
class TestIncrementalWindow(object):

    def _harvester(self, config=None):
        return _new_harvester(config=config)

    def test_not_supported_returns_empty_window(self):
        class PlainHarvester(HarvesterBase):
            pass

        harvester = _new_harvester(PlainHarvester)
        job = _job(_source('http://not-supported.test/'))
        assert harvester._get_incremental_window(job) == (None, None)

    def test_first_run_has_no_since_but_records_until(self):
        harvester = self._harvester()
        job = _job(_source('http://first-run.test/'))
        now = datetime.datetime(2026, 11, 23, 12, 0, 0)

        since, until = harvester._get_incremental_window(job, now=now)

        assert since is None
        assert until == now

    def test_resume_uses_overlap_and_safety_margin(self):
        source = _source('http://resume.test/')
        checkpoint = datetime.datetime(2026, 11, 23, 12, 0, 0)
        source.incremental_checkpoint = checkpoint
        source.save()
        job = _job(source)

        harvester = self._harvester({'incremental_overlap': 30})
        now = datetime.datetime(2026, 11, 23, 13, 0, 0)

        since, until = harvester._get_incremental_window(job, now=now)

        assert until == now
        assert since == checkpoint - datetime.timedelta(minutes=30) \
            - INCREMENTAL_SAFETY_MARGIN

    def test_force_all_skips_incremental_request(self):
        source = _source('http://force.test/')
        source.incremental_checkpoint = datetime.datetime(2026, 11, 23, 12)
        source.save()
        job = _job(source)

        harvester = self._harvester({'force_all': True})
        since, until = harvester._get_incremental_window(job)

        assert since is None
        assert until is not None

    def test_inverted_window_is_never_returned(self):
        source = _source('http://inverted.test/')
        now = datetime.datetime(2026, 11, 23, 12, 0, 0)
        # Checkpoint is in the future relative to ``now`` (clock skew).
        source.incremental_checkpoint = now + datetime.timedelta(hours=2)
        source.save()
        job = _job(source)

        harvester = self._harvester({'incremental_overlap': 10})
        since, until = harvester._get_incremental_window(job, now=now)

        assert since < until
        assert until == now


@pytest.mark.usefixtures('with_plugins', 'clean_db')
class TestIncrementalOverlapConfig(object):

    def _overlap(self, config=None, global_config=None):
        harvester = _new_harvester(config=config)
        if global_config is None:
            return harvester._get_incremental_overlap()
        with helpers.changed_config(
                'ckan.harvest.incremental_overlap', global_config):
            return harvester._get_incremental_overlap()

    def test_default_overlap(self):
        assert self._overlap() == datetime.timedelta(
            minutes=DEFAULT_INCREMENTAL_OVERLAP_MINUTES)

    def test_source_config_takes_precedence(self):
        assert self._overlap(
            {'incremental_overlap': 15}, global_config='90'
        ) == datetime.timedelta(minutes=15)

    def test_global_config_used_when_source_unset(self):
        assert self._overlap(
            {}, global_config='90') == datetime.timedelta(minutes=90)

    def test_invalid_overlap_falls_back_to_default(self):
        assert self._overlap(
            {'incremental_overlap': 'not-a-number'}) == datetime.timedelta(
            minutes=DEFAULT_INCREMENTAL_OVERLAP_MINUTES)

    def test_negative_overlap_is_clamped_to_zero(self):
        assert self._overlap(
            {'incremental_overlap': -5}) == datetime.timedelta(minutes=0)


@pytest.mark.usefixtures('with_plugins', 'clean_db')
class TestCheckpointAdvancement(object):

    def _advance(self, job, config=None):
        harvester = _new_harvester(config=config)
        return harvester.advance_incremental_checkpoint(job)

    def test_advances_when_all_objects_complete(self):
        source = _source('http://advance.test/')
        until = datetime.datetime(2026, 11, 23, 12)
        job = _job(source, until)
        _object(job, 'guid-1', 'COMPLETE')
        _object(job, 'guid-2', 'COMPLETE')

        assert self._advance(job) == until

        source = harvest_model.HarvestSource.get(source.id)
        assert source.incremental_checkpoint == until

    def test_does_not_advance_when_an_object_errored(self):
        source = _source('http://advance-error.test/')
        until = datetime.datetime(2026, 11, 23, 12)
        job = _job(source, until)
        _object(job, 'guid-1', 'COMPLETE')
        _object(job, 'guid-2', 'ERROR')

        assert self._advance(job) is None

        source = harvest_model.HarvestSource.get(source.id)
        assert source.incremental_checkpoint is None

    def test_does_not_advance_when_an_object_is_in_flight(self):
        source = _source('http://advance-pending.test/')
        until = datetime.datetime(2026, 11, 23, 12)
        job = _job(source, until)
        _object(job, 'guid-1', 'COMPLETE')
        _object(job, 'guid-2', 'WAITING')

        assert self._advance(job) is None

        source = harvest_model.HarvestSource.get(source.id)
        assert source.incremental_checkpoint is None

    def test_does_not_advance_on_gather_error(self):
        source = _source('http://advance-gather-error.test/')
        until = datetime.datetime(2026, 11, 23, 12)
        job = _job(source, until)
        _object(job, 'guid-1', 'COMPLETE')
        harvest_model.HarvestGatherError.create('boom', job)

        assert self._advance(job) is None

    def test_job_without_window_does_not_advance(self):
        source = _source('http://advance-no-window.test/')
        job = _job(source)
        _object(job, 'guid-1', 'COMPLETE')

        assert self._advance(job) is None

    def test_checkpoint_never_moves_backwards(self):
        source = _source('http://advance-backwards.test/')
        later = datetime.datetime(2026, 11, 23, 13)
        source.incremental_checkpoint = later
        source.save()

        job = _job(source, datetime.datetime(2026, 11, 23, 12))
        _object(job, 'guid-1', 'COMPLETE')

        assert self._advance(job) == later

        source = harvest_model.HarvestSource.get(source.id)
        assert source.incremental_checkpoint == later


@pytest.mark.usefixtures('with_plugins', 'clean_db')
class TestGatherStageWindowPersistence(object):

    def test_exception_clears_window_and_deletes_objects(self):
        from ckanext.harvest import queue

        source = _source('http://gather-exception.test/')

        class BrokenHarvester(IncrementalHarvester):
            def gather_stage(self, harvest_job):
                self._set_incremental_window(
                    harvest_job,
                    datetime.datetime.utcnow(),
                    datetime.datetime.utcnow())
                harvest_job.save()
                _object(harvest_job, 'partial', 'WAITING')
                raise RuntimeError('boom')

        harvester = _new_harvester(BrokenHarvester)
        job = _job(source)

        with pytest.raises(RuntimeError):
            queue.gather_stage(harvester, job)

        job = harvest_model.HarvestJob.get(job.id)
        assert job.gather_finished is not None
        # The aborted window must not be eligible for checkpoint advancement.
        assert job.incremental_since is None
        assert job.incremental_until is None
        assert harvest_model.HarvestObject.filter(
            harvest_job_id=job.id).count() == 0

    def test_success_keeps_window(self):
        from ckanext.harvest import queue

        source = _source('http://gather-success.test/')
        until = datetime.datetime.utcnow()

        class GoodHarvester(IncrementalHarvester):
            def gather_stage(self, harvest_job):
                self._set_incremental_window(harvest_job, None, until)
                return []

        harvester = _new_harvester(GoodHarvester)
        job = _job(source)

        queue.gather_stage(harvester, job)

        job = harvest_model.HarvestJob.get(job.id)
        assert job.incremental_until == until


@pytest.mark.usefixtures('with_plugins', 'clean_db', 'clean_index')
class TestSourceClearResetsCheckpoint(object):

    def test_clear_resets_incremental_checkpoint(self):
        source = _source('http://clear-reset.test/')
        source.incremental_checkpoint = datetime.datetime(2026, 11, 23, 12)
        source.save()

        context = {'model': model, 'session': model.Session,
                   'ignore_auth': True, 'user': ''}
        toolkit.get_action('harvest_source_clear')(
            context, {'id': source.id})

        source = harvest_model.HarvestSource.get(source.id)
        assert source.incremental_checkpoint is None


@pytest.mark.usefixtures('with_plugins', 'clean_db', 'clean_index')
class TestCkanHarvesterIncremental(object):

    def test_first_harvest_is_full_and_records_until(self):
        source = _source('http://ckan-incremental.test/')
        job = _job(source)

        captured = {}

        def fake_search(self, base_url, fq_terms=None):
            captured['fq_terms'] = fq_terms
            return []

        harvester = CKANHarvester()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(CKANHarvester, '_search_for_datasets', fake_search)
            harvester.gather_stage(job)

        # The first run is a full harvest - no metadata_modified filter.
        assert not any('metadata_modified' in (term or '')
                       for term in (captured['fq_terms'] or []))

        job = harvest_model.HarvestJob.get(job.id)
        assert job.incremental_since is None
        assert job.incremental_until is not None

    def test_resume_query_includes_overlap_window(self):
        checkpoint = datetime.datetime.utcnow() - datetime.timedelta(days=1)
        source = _source('http://ckan-resume.test/')
        source.incremental_checkpoint = checkpoint
        source.save()
        job = _job(source)

        captured = {}

        def fake_search(self, base_url, fq_terms=None):
            captured['fq_terms'] = fq_terms
            return []

        harvester = CKANHarvester()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(CKANHarvester, '_search_for_datasets', fake_search)
            # ``gather_stage`` reads the config from the source itself.
            source.config = json.dumps({'incremental_overlap': 30})
            source.save()
            harvester.gather_stage(job)

        assert captured['fq_terms'] is not None
        assert len(captured['fq_terms']) == 1
        fq = captured['fq_terms'][0]
        assert fq.startswith('metadata_modified:[')
        assert fq.endswith('Z TO *]')

        expected_since = checkpoint - datetime.timedelta(minutes=30) \
            - INCREMENTAL_SAFETY_MARGIN
        assert expected_since.isoformat() + 'Z' in fq

        # The job persists the window bounds for later checkpoint handling.
        job = harvest_model.HarvestJob.get(job.id)
        assert job.incremental_since == expected_since
        assert job.incremental_until is not None

    def test_gather_creates_objects_for_same_timestamp_records(self):
        checkpoint = datetime.datetime.utcnow() - datetime.timedelta(days=1)
        source = _source('http://ckan-same-ts.test/')
        source.incremental_checkpoint = checkpoint
        source.save()
        job = _job(source)

        # Two distinct datasets sharing the exact same modification time.
        stamp = '2026-11-23T12:05:00.000000'
        pkg_dicts = [
            {'id': 'aaa', 'name': 'dataset-aaa', 'metadata_modified': stamp},
            {'id': 'bbb', 'name': 'dataset-bbb', 'metadata_modified': stamp},
        ]

        def fake_search(self, base_url, fq_terms=None):
            return pkg_dicts

        harvester = CKANHarvester()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(CKANHarvester, '_search_for_datasets', fake_search)
            obj_ids = harvester.gather_stage(job)

        guids = {
            harvest_model.HarvestObject.get(obj_id).guid
            for obj_id in obj_ids
        }
        assert guids == {'aaa', 'bbb'}

    def test_gather_error_falls_back_to_full_harvest(self):
        checkpoint = datetime.datetime.utcnow() - datetime.timedelta(days=1)
        source = _source('http://ckan-gather-error.test/')
        source.incremental_checkpoint = checkpoint
        source.save()
        job = _job(source)

        from ckanext.harvest.harvesters.ckanharvester import SearchError

        calls = []

        def failing_range_then_full(self, base_url, fq_terms=None):
            calls.append(fq_terms)
            has_range = any('metadata_modified' in (term or '')
                            for term in (fq_terms or []))
            if has_range:
                raise SearchError('range query rejected')
            # Full harvest succeeds and returns one dataset.
            return [{'id': 'full-dataset', 'name': 'full-dataset'}]

        harvester = CKANHarvester()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(CKANHarvester, '_search_for_datasets',
                       failing_range_then_full)
            result = harvester.gather_stage(job)

        # First the bounded query, then the unfiltered full harvest.
        assert len(calls) == 2
        assert any('metadata_modified' in (t or '') for t in (calls[0] or []))
        assert not any('metadata_modified' in (t or '')
                       for t in (calls[1] or []))
        # No gather error is recorded; the checkpoint is left untouched so
        # the window can still be advanced after the full harvest completes.
        assert job.gather_errors == []
        source = harvest_model.HarvestSource.get(source.id)
        assert source.incremental_checkpoint == checkpoint

    def test_failed_import_keeps_window_for_rerun(self):
        checkpoint = datetime.datetime(2026, 11, 23, 12, 0, 0)
        source = _source('http://ckan-import-error.test/')
        source.incremental_checkpoint = checkpoint
        source.save()

        job = _job(source, checkpoint + datetime.timedelta(minutes=5),
                   checkpoint, status='Running')
        _object(job, 'ok', 'COMPLETE')
        _object(job, 'bad', 'ERROR')

        context = {'model': model, 'session': model.Session,
                   'ignore_auth': True, 'user': ''}
        toolkit.get_action('harvest_jobs_run')(context, {})

        job = harvest_model.HarvestJob.get(job.id)
        assert job.status == 'Finished'
        source = harvest_model.HarvestSource.get(source.id)
        assert source.incremental_checkpoint == checkpoint

    def test_successful_job_advances_checkpoint(self):
        checkpoint = datetime.datetime(2026, 11, 23, 12, 0, 0)
        until = checkpoint + datetime.timedelta(minutes=5)
        source = _source('http://ckan-advance.test/')
        source.incremental_checkpoint = checkpoint
        source.save()

        job = _job(source, until, checkpoint, status='Running')
        _object(job, 'ok', 'COMPLETE')

        context = {'model': model, 'session': model.Session,
                   'ignore_auth': True, 'user': ''}
        toolkit.get_action('harvest_jobs_run')(context, {})

        source = harvest_model.HarvestSource.get(source.id)
        assert source.incremental_checkpoint == until


@pytest.mark.usefixtures('with_plugins', 'clean_db', 'clean_index')
class TestCkanHarvesterIncrementalIntegration(object):

    def test_two_runs_advance_checkpoint(self):
        url = 'http://localhost:%s/' % mock_ckan.PORT
        run_harvest(url=url, harvester=CKANHarvester())

        source = harvest_model.HarvestSource.filter(url=url).one()
        first_checkpoint = source.incremental_checkpoint
        assert first_checkpoint is not None

        # Second run must resume from the checkpoint.
        run_harvest(url=url, harvester=CKANHarvester())

        source = harvest_model.HarvestSource.get(source.id)
        assert source.incremental_checkpoint >= first_checkpoint


@pytest.mark.usefixtures('with_plugins', 'clean_db')
class TestValidateIncrementalConfig(object):

    def test_accepts_numeric_overlap(self):
        config = CKANHarvester().validate_config(
            '{"incremental_overlap": 30}')
        assert 'incremental_overlap' in config

    def test_rejects_non_numeric_overlap(self):
        with pytest.raises(ValueError):
            CKANHarvester().validate_config(
                '{"incremental_overlap": "soon"}')

    def test_rejects_negative_overlap(self):
        with pytest.raises(ValueError):
            CKANHarvester().validate_config(
                '{"incremental_overlap": -1}')
