# -*- coding: utf-8 -*-

import datetime

import pytest

from ckantoolkit.tests import factories as ckan_factories, helpers
from ckantoolkit import ValidationError

from ckan import model as ckan_model
from ckan.plugins import toolkit

import ckanext.harvest.model as harvest_model
from ckanext.harvest.tests import factories


def _dt(day, hour=0, minute=0):
    '''Fixed timestamps so the snapshot moment is fully deterministic.'''
    return datetime.datetime(2024, 3, day, hour, minute)


AT = _dt(2)  # the snapshot moment used by most tests: 2024-03-02T00:00:00


def _make_source(**kwargs):
    kwargs.setdefault('source_type', 'test')
    return factories.HarvestSourceObj(**kwargs)


def _make_job(source, created, status=u'Finished', finished=None):
    job = harvest_model.HarvestJob(
        source_id=source.id,
        status=status,
        created=created,
        finished=finished,
    )
    job.save()
    return job


def _make_object(job, guid, gathered, state=u'COMPLETE', import_finished=None,
                 package_id=None, report_status=u'added',
                 metadata_modified_date=None):
    obj = harvest_model.HarvestObject(
        guid=guid,
        job=job,
        harvest_source_id=job.source_id,
        gathered=gathered,
        state=state,
        import_finished=import_finished,
        package_id=package_id,
        report_status=report_status,
        metadata_modified_date=metadata_modified_date,
    )
    obj.save()
    return obj


def _catalog(source_id, **kwargs):
    return helpers.call_action(
        'harvest_source_catalog_at', id=source_id, **kwargs)


@pytest.mark.usefixtures('with_plugins', 'clean_db', 'clean_queues')
@pytest.mark.ckan_config('ckan.plugins', 'harvest test_harvester')
class TestHarvestSourceCatalogAt(object):

    def test_available_entry_has_local_id_and_version(self):
        source = _make_source()
        dataset = ckan_factories.Dataset()
        job = _make_job(source, created=_dt(1, 8), finished=_dt(1, 11))
        obj = _make_object(
            job, 'guid-1', gathered=_dt(1, 9), import_finished=_dt(1, 10),
            package_id=dataset['id'], report_status='added',
            metadata_modified_date=_dt(1, 7))

        result = _catalog(source.id, at='2024-03-02T00:00:00')

        assert result['source_id'] == source.id
        assert result['at'] == '2024-03-02T00:00:00'
        assert result['count'] == 1
        assert result['summary'] == {
            'available': 1, 'pending': 0, 'withdrawn': 0}

        entry = result['results'][0]
        assert entry['guid'] == 'guid-1'
        assert entry['status'] == 'available'
        assert entry['package_id'] == dataset['id']
        assert entry['harvest_object_id'] == obj.id
        assert entry['report_status'] == 'added'
        assert entry['import_finished'] == _dt(1, 10).isoformat()
        assert entry['metadata_modified_date'] == _dt(1, 7).isoformat()

        # A finished job covers the snapshot, so there are no gaps
        assert result['gaps'] == []
        assert result['coverage']['last_job_finished_before'] == {
            'id': job.id, 'finished': _dt(1, 11).isoformat()}
        assert result['coverage']['first_job_created_after'] is None

    def test_source_can_be_referenced_by_name(self):
        source = _make_source(name='catalog-at-source')

        result = _catalog('catalog-at-source', at=AT.isoformat())

        assert result['source_id'] == source.id
        assert result['count'] == 0

    def test_pending_when_object_not_imported_yet(self):
        source = _make_source()
        job = _make_job(source, created=_dt(1, 8), finished=_dt(1, 11))
        _make_object(job, 'guid-waiting', gathered=_dt(1, 9), state='WAITING',
                     report_status=None)

        result = _catalog(source.id, at=AT.isoformat())

        assert result['count'] == 1
        entry = result['results'][0]
        assert entry['status'] == 'pending'
        assert entry['state'] == 'WAITING'
        assert entry['package_id'] is None
        assert entry['harvest_object_id'] is None
        assert result['summary']['pending'] == 1

    def test_pending_when_import_finished_after_snapshot(self):
        source = _make_source()
        dataset = ckan_factories.Dataset()
        job = _make_job(source, created=_dt(1, 8), finished=_dt(1, 11))
        # Gathered before the snapshot but only imported after it
        _make_object(job, 'guid-late', gathered=_dt(1, 9),
                     import_finished=_dt(3), package_id=dataset['id'])

        result = _catalog(source.id, at=AT.isoformat())

        entry = result['results'][0]
        assert entry['status'] == 'pending'
        assert entry['package_id'] is None

    def test_pending_when_import_errored(self):
        source = _make_source()
        job = _make_job(source, created=_dt(1, 8), finished=_dt(1, 11))
        _make_object(job, 'guid-broken', gathered=_dt(1, 9), state='ERROR',
                     import_finished=_dt(1, 10), report_status='errored')

        result = _catalog(source.id, at=AT.isoformat())

        entry = result['results'][0]
        assert entry['status'] == 'pending'
        assert entry['report_status'] == 'errored'
        assert result['summary'] == {
            'available': 0, 'pending': 1, 'withdrawn': 0}

    def test_withdrawn_when_last_import_marked_deleted(self):
        source = _make_source()
        dataset = ckan_factories.Dataset()
        job1 = _make_job(source, created=_dt(1, 8), finished=_dt(1, 11))
        _make_object(job1, 'guid-1', gathered=_dt(1, 9),
                     import_finished=_dt(1, 10),
                     package_id=dataset['id'], report_status='added')
        # A later job reports the record as deleted; deletion markers may
        # not carry the package id themselves
        job2 = _make_job(source, created=_dt(2, 8), finished=_dt(2, 11))
        _make_object(job2, 'guid-1', gathered=_dt(2, 9),
                     import_finished=_dt(2, 10),
                     package_id=None, report_status='deleted')

        result = _catalog(source.id, at=_dt(3).isoformat())

        entry = result['results'][0]
        assert entry['status'] == 'withdrawn'
        assert entry['report_status'] == 'deleted'
        # The local id is recovered from the previous import
        assert entry['package_id'] == dataset['id']
        assert result['summary'] == {
            'available': 0, 'pending': 0, 'withdrawn': 1}

    def test_version_in_effect_depends_on_snapshot_moment(self):
        source = _make_source()
        dataset = ckan_factories.Dataset()
        job1 = _make_job(source, created=_dt(1, 8), finished=_dt(1, 11))
        obj1 = _make_object(job1, 'guid-1', gathered=_dt(1, 9),
                            import_finished=_dt(1, 10),
                            package_id=dataset['id'], report_status='added')
        job2 = _make_job(source, created=_dt(3, 8), finished=_dt(3, 11))
        obj2 = _make_object(job2, 'guid-1', gathered=_dt(3, 9),
                            import_finished=_dt(3, 10),
                            package_id=dataset['id'], report_status='updated')

        before_update = _catalog(source.id, at=_dt(2).isoformat())
        after_update = _catalog(source.id, at=_dt(4).isoformat())

        assert before_update['results'][0]['harvest_object_id'] == obj1.id
        assert before_update['results'][0]['report_status'] == 'added'
        assert after_update['results'][0]['harvest_object_id'] == obj2.id
        assert after_update['results'][0]['report_status'] == 'updated'

    def test_not_modified_import_keeps_previous_version(self):
        source = _make_source()
        dataset = ckan_factories.Dataset()
        job1 = _make_job(source, created=_dt(1, 8), finished=_dt(1, 11))
        obj1 = _make_object(job1, 'guid-1', gathered=_dt(1, 9),
                            import_finished=_dt(1, 10),
                            package_id=dataset['id'], report_status='added')
        job2 = _make_job(source, created=_dt(3, 8), finished=_dt(3, 11))
        # "not modified" objects do not set the local state (no package id)
        _make_object(job2, 'guid-1', gathered=_dt(3, 9),
                     import_finished=_dt(3, 10),
                     package_id=None, report_status='not modified')

        result = _catalog(source.id, at=_dt(4).isoformat())

        entry = result['results'][0]
        assert entry['status'] == 'available'
        assert entry['harvest_object_id'] == obj1.id
        assert entry['package_id'] == dataset['id']

    def test_guid_gathered_after_snapshot_is_not_listed(self):
        source = _make_source()
        job = _make_job(source, created=_dt(1, 8), finished=_dt(1, 11))
        _make_object(job, 'guid-old', gathered=_dt(1, 9),
                     import_finished=_dt(1, 10),
                     package_id=ckan_factories.Dataset()['id'])
        _make_object(job, 'guid-future', gathered=_dt(5),
                     import_finished=_dt(5, 1),
                     package_id=ckan_factories.Dataset()['id'])

        result = _catalog(source.id, at=AT.isoformat())

        assert result['count'] == 1
        assert result['results'][0]['guid'] == 'guid-old'

    def test_gaps_report_jobs_not_finished_at_snapshot(self):
        source = _make_source()
        finished_late = _make_job(source, created=_dt(1, 8),
                                  finished=_dt(3))
        _make_object(finished_late, 'guid-1', gathered=_dt(1, 9),
                     state='WAITING', report_status=None)
        still_running = _make_job(source, created=_dt(1, 9),
                                  status='Running', finished=None)
        aborted = _make_job(source, created=_dt(1, 10),
                            status='Aborted', finished=None)

        result = _catalog(source.id, at=AT.isoformat())

        gap_reasons = {gap['job_id']: gap['reason'] for gap in result['gaps']
                       if gap['type'] == 'unfinished_job'}
        assert gap_reasons == {
            finished_late.id: 'job_finished_after_snapshot',
            still_running.id: 'job_not_finished',
            aborted.id: 'job_aborted',
        }
        # No job had finished by the snapshot moment
        no_coverage = [gap for gap in result['gaps']
                       if gap['type'] == 'no_finished_job_before_snapshot']
        assert len(no_coverage) == 1
        assert no_coverage[0]['objects_gathered'] == 1
        assert result['coverage']['last_job_finished_before'] is None

    def test_no_gap_when_job_finished_before_snapshot(self):
        source = _make_source()
        _make_job(source, created=_dt(1, 8), finished=_dt(1, 11))

        result = _catalog(source.id, at=AT.isoformat())

        assert result['gaps'] == []

    def test_coverage_reports_next_job_after_snapshot(self):
        source = _make_source()
        _make_job(source, created=_dt(1, 8), finished=_dt(1, 11))
        next_job = _make_job(source, created=_dt(5, 8), finished=_dt(5, 11))

        result = _catalog(source.id, at=AT.isoformat())

        assert result['coverage']['first_job_created_after'] == {
            'id': next_job.id, 'created': _dt(5, 8).isoformat()}

    def test_pagination_is_stable_and_verifiable(self):
        source = _make_source()
        job = _make_job(source, created=_dt(1, 8), finished=_dt(1, 11))
        datasets = [ckan_factories.Dataset() for _ in range(5)]
        # Insert in non-alphabetical order on purpose
        for i, guid in enumerate(['guid-c', 'guid-a', 'guid-e', 'guid-b',
                                  'guid-d']):
            _make_object(job, guid, gathered=_dt(1, 9),
                         import_finished=_dt(1, 10),
                         package_id=datasets[i]['id'])

        pages = [_catalog(source.id, at=AT.isoformat(), limit=2, offset=o)
                 for o in (0, 2, 4)]

        assert [page['count'] for page in pages] == [5, 5, 5]
        assert [len(page['results']) for page in pages] == [2, 2, 1]

        # Deterministic, guid-sorted order across pages
        guids = [entry['guid'] for page in pages for entry in page['results']]
        assert guids == ['guid-a', 'guid-b', 'guid-c', 'guid-d', 'guid-e']

        # Every page of the same reconstruction carries the same hash...
        assert len({page['result_hash'] for page in pages}) == 1

        # ...and a full re-run produces the identical listing and hash
        full = _catalog(source.id, at=AT.isoformat(), limit=100)
        assert full['result_hash'] == pages[0]['result_hash']
        assert [e['guid'] for e in full['results']] == guids

        # The hash changes when the underlying catalog changes
        earlier = _catalog(source.id, at=_dt(1, 9, 30).isoformat())
        assert earlier['result_hash'] != full['result_hash']

    def test_status_filter(self):
        source = _make_source()
        job = _make_job(source, created=_dt(1, 8), finished=_dt(1, 11))
        _make_object(job, 'guid-ok', gathered=_dt(1, 9),
                     import_finished=_dt(1, 10),
                     package_id=ckan_factories.Dataset()['id'])
        _make_object(job, 'guid-stuck', gathered=_dt(1, 9), state='WAITING',
                     report_status=None)

        result = _catalog(source.id, at=AT.isoformat(), status='pending')

        assert result['count'] == 1
        assert result['results'][0]['guid'] == 'guid-stuck'
        # The summary always describes the full catalog
        assert result['summary'] == {
            'available': 1, 'pending': 1, 'withdrawn': 0}

    def test_missing_at_raises_validation_error(self):
        source = _make_source()

        with pytest.raises(ValidationError) as excinfo:
            _catalog(source.id)

        assert 'at' in excinfo.value.error_dict

    def test_invalid_at_raises_validation_error(self):
        source = _make_source()

        with pytest.raises(ValidationError) as excinfo:
            _catalog(source.id, at='not-a-date')

        assert 'at' in excinfo.value.error_dict

    def test_invalid_status_raises_validation_error(self):
        source = _make_source()

        with pytest.raises(ValidationError) as excinfo:
            _catalog(source.id, at=AT.isoformat(), status='deleted')

        assert 'status' in excinfo.value.error_dict

    def test_invalid_pagination_raises_validation_error(self):
        source = _make_source()

        with pytest.raises(ValidationError):
            _catalog(source.id, at=AT.isoformat(), limit='lots')
        with pytest.raises(ValidationError):
            _catalog(source.id, at=AT.isoformat(), offset=-1)

    def test_missing_id_raises_validation_error(self):
        with pytest.raises(ValidationError) as excinfo:
            helpers.call_action('harvest_source_catalog_at',
                                at=AT.isoformat())

        assert 'id' in excinfo.value.error_dict

    def test_unknown_source_raises_not_found(self):
        with pytest.raises(toolkit.ObjectNotFound):
            _catalog('no-such-source', at=AT.isoformat())

    def test_auth_forwards_to_harvest_source_show(self):
        source = _make_source()

        # Anonymous users can read the catalog of a public source
        context = {'model': ckan_model, 'user': ''}
        assert helpers.call_auth(
            'harvest_source_catalog_at', context=context,
            id=source.id, at=AT.isoformat()) is True

    def test_timezone_aware_at_is_normalized_to_utc(self):
        source = _make_source()
        dataset = ckan_factories.Dataset()
        job = _make_job(source, created=_dt(1, 8), finished=_dt(1, 11))
        _make_object(job, 'guid-1', gathered=_dt(1, 9),
                     import_finished=_dt(1, 10),
                     package_id=dataset['id'])

        # 2024-03-02T02:00:00+02:00 == 2024-03-02T00:00:00 UTC
        result = _catalog(source.id, at='2024-03-02T02:00:00+02:00')

        assert result['at'] == '2024-03-02T00:00:00'
        assert result['count'] == 1
