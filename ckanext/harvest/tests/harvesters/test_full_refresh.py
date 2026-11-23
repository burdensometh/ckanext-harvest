'''Tests for full refresh (complete inventory) harvest jobs retracting
datasets that have been withdrawn from the remote source.
'''
import pytest

from ckan import model
from ckan.plugins import toolkit

from ckanext.harvest.model import HarvestObject, HarvestJob, HarvestGatherError
from ckanext.harvest.harvesters.base import HarvesterBase
from ckanext.harvest.tests.factories import (HarvestSourceObj, HarvestJobObj)
from ckanext.harvest.tests.lib import run_harvest_job


class FullRefreshMockHarvester(HarvesterBase):
    '''A harvester whose gathered identities and full-refresh behaviour are
    configured per test through class attributes.
    '''
    # list of guids to gather on the next gather_stage
    guids = []
    # whether the next gather is a complete inventory (full refresh)
    full_refresh = True
    # make every other object fail its import stage
    fail_guids = []
    # record a gather error on the next gather
    gather_error = False

    @classmethod
    def reset(cls):
        cls.guids = []
        cls.full_refresh = True
        cls.fail_guids = []
        cls.gather_error = False

    def info(self):
        return {'name': 'test_full_refresh', 'title': 'test',
                'description': 'test'}

    def gather_stage(self, harvest_job):
        harvest_job.full_refresh = self.full_refresh
        harvest_job.save()
        if self.gather_error:
            HarvestGatherError.create(
                'Simulated gather failure', harvest_job)
        object_ids = []
        for guid in self.guids:
            obj = HarvestObject(guid=guid, job=harvest_job,
                                content='{"name": "%s"}' % guid)
            obj.save()
            object_ids.append(obj.id)
        return object_ids

    def fetch_stage(self, harvest_object):
        return True

    def import_stage(self, harvest_object):
        user = toolkit.get_action('get_site_user')(
            {'model': model, 'ignore_auth': True}, {}
        )['name']
        context = {'model': model, 'session': model.Session,
                   'user': user, 'ignore_auth': True}

        if harvest_object.guid in self.fail_guids:
            return False

        package = model.Package.get(harvest_object.guid)
        if package:
            package_dict = toolkit.get_action('package_update')(
                context, {'id': package.id, 'name': harvest_object.guid})
        else:
            package_dict = toolkit.get_action('package_create')(
                context, {'name': harvest_object.guid})

        previous = model.Session.query(HarvestObject) \
            .filter_by(guid=harvest_object.guid) \
            .filter_by(current=True).first()
        if previous:
            previous.current = False
            previous.save()

        harvest_object.package_id = package_dict['id']
        harvest_object.current = True
        harvest_object.save()
        return True


@pytest.mark.usefixtures('with_plugins', 'clean_db', 'clean_queues',
                         'clean_index')
@pytest.mark.ckan_config(
    'ckan.plugins', 'harvest test_full_refresh_harvester')
class TestFullRefreshCleanup(object):

    def setup_method(self):
        FullRefreshMockHarvester.reset()

    def _run_job(self, source=None):
        if source is None:
            source = HarvestSourceObj(
                url='http://full-refresh.example.com',
                source_type='test_full_refresh')
        job = HarvestJobObj(source=source, run=False)
        run_harvest_job(job, FullRefreshMockHarvester())
        return HarvestJob.get(job.id), source

    def _package_state(self, name):
        pkg = model.Package.get(name)
        return pkg.state if pkg else None

    def test_full_refresh_deletes_withdrawn_datasets(self):
        # First full refresh: two remote datasets
        FullRefreshMockHarvester.guids = ['dataset-a', 'dataset-b']
        self._run_job()
        assert self._package_state('dataset-a') == 'active'
        assert self._package_state('dataset-b') == 'active'

        # Second full refresh: dataset-b has been withdrawn remotely
        FullRefreshMockHarvester.guids = ['dataset-a']
        job, source = self._run_job()

        assert job.full_refresh is True
        assert self._package_state('dataset-a') == 'active'
        assert self._package_state('dataset-b') == 'deleted'

        # The withdrawn dataset no longer appears in the catalog results
        results = toolkit.get_action('package_search')(
            {'ignore_auth': True},
            {'fq': 'name:dataset-b'})
        assert results['count'] == 0

        # Re-running the search for the datasets that remain still works
        results = toolkit.get_action('package_search')(
            {'ignore_auth': True},
            {'fq': 'name:dataset-a'})
        assert results['count'] == 1
        assert results['results'][0]['state'] == 'active'

        # The withdrawal is recorded on the job with a 'deleted' object
        deletion_objects = model.Session.query(HarvestObject) \
            .filter_by(harvest_job_id=job.id) \
            .filter_by(report_status='deleted').all()
        assert len(deletion_objects) == 1
        assert deletion_objects[0].guid == 'dataset-b'
        assert deletion_objects[0].current is False

        job_dict = toolkit.get_action('harvest_job_show')(
            {'ignore_auth': True}, {'id': job.id})
        assert job_dict['stats']['deleted'] == 1

        status = toolkit.get_action('harvest_source_show_status')(
            {'ignore_auth': True}, {'id': source.id})
        assert status['total_datasets'] == 1

    def test_incremental_job_keeps_withdrawn_datasets(self):
        FullRefreshMockHarvester.guids = ['dataset-a', 'dataset-b']
        self._run_job()

        # Incremental job only lists changed datasets, so its identity list
        # is incomplete and must not trigger any deletion
        FullRefreshMockHarvester.full_refresh = False
        FullRefreshMockHarvester.guids = ['dataset-a']
        job, _ = self._run_job()

        assert job.full_refresh is False
        assert self._package_state('dataset-a') == 'active'
        assert self._package_state('dataset-b') == 'active'

        deletion_objects = model.Session.query(HarvestObject) \
            .filter_by(harvest_job_id=job.id) \
            .filter_by(report_status='deleted').all()
        assert deletion_objects == []

    def test_failed_full_refresh_keeps_withdrawn_datasets(self):
        FullRefreshMockHarvester.guids = ['dataset-a', 'dataset-b']
        self._run_job()

        # A full refresh where one object fails to import: the identity list
        # cannot be trusted, so no dataset must be retracted
        FullRefreshMockHarvester.guids = ['dataset-a', 'dataset-b']
        FullRefreshMockHarvester.fail_guids = ['dataset-b']
        job, _ = self._run_job()

        assert job.full_refresh is True
        assert self._package_state('dataset-a') == 'active'
        assert self._package_state('dataset-b') == 'active'

        deletion_objects = model.Session.query(HarvestObject) \
            .filter_by(harvest_job_id=job.id) \
            .filter_by(report_status='deleted').all()
        assert deletion_objects == []

    def test_gather_error_keeps_withdrawn_datasets(self):
        FullRefreshMockHarvester.guids = ['dataset-a', 'dataset-b']
        self._run_job()

        # A full refresh whose gather reported an error: the inventory is
        # not trustworthy, so no dataset must be retracted
        FullRefreshMockHarvester.guids = ['dataset-a']
        FullRefreshMockHarvester.gather_error = True
        job, _ = self._run_job()

        assert job.full_refresh is True
        assert self._package_state('dataset-a') == 'active'
        assert self._package_state('dataset-b') == 'active'

        deletion_objects = model.Session.query(HarvestObject) \
            .filter_by(harvest_job_id=job.id) \
            .filter_by(report_status='deleted').all()
        assert deletion_objects == []

    def test_cleanup_is_scoped_to_the_source(self):
        # Source 1 holds dataset-a and dataset-b
        source1 = HarvestSourceObj(
            url='http://source1.example.com',
            source_type='test_full_refresh')
        FullRefreshMockHarvester.guids = ['dataset-a', 'dataset-b']
        job1 = HarvestJobObj(source=source1, run=False)
        run_harvest_job(job1, FullRefreshMockHarvester())

        # Source 2 holds dataset-c
        source2 = HarvestSourceObj(
            url='http://source2.example.com',
            source_type='test_full_refresh')
        FullRefreshMockHarvester.guids = ['dataset-c']
        job2 = HarvestJobObj(source=source2, run=False)
        run_harvest_job(job2, FullRefreshMockHarvester())

        # Source 1 full refresh now only lists dataset-a
        FullRefreshMockHarvester.guids = ['dataset-a']
        job1 = HarvestJobObj(source=source1, run=False)
        run_harvest_job(job1, FullRefreshMockHarvester())

        assert self._package_state('dataset-a') == 'active'
        assert self._package_state('dataset-b') == 'deleted'
        # Source 2's dataset is untouched
        assert self._package_state('dataset-c') == 'active'

    def test_full_refresh_without_changes_deletes_nothing(self):
        FullRefreshMockHarvester.guids = ['dataset-a', 'dataset-b']
        self._run_job()

        FullRefreshMockHarvester.guids = ['dataset-a', 'dataset-b']
        job, _ = self._run_job()

        assert self._package_state('dataset-a') == 'active'
        assert self._package_state('dataset-b') == 'active'
        job_dict = toolkit.get_action('harvest_job_show')(
            {'ignore_auth': True}, {'id': job.id})
        assert job_dict['stats']['deleted'] == 0

    def test_persisted_identities_used_for_cleanup(self):
        # The gathered identities must be persisted: simulate the framework
        # deciding on cleanup purely from the HarvestObject rows of the job
        FullRefreshMockHarvester.guids = ['dataset-a', 'dataset-b']
        job, _ = self._run_job()

        guids = set(
            guid for guid, in model.Session.query(HarvestObject.guid)
            .filter_by(harvest_job_id=job.id).all())
        assert guids == {'dataset-a', 'dataset-b'}

    def test_harvest_job_dict_includes_full_refresh_flag(self):
        FullRefreshMockHarvester.guids = ['dataset-a']
        job, _ = self._run_job()
        job_dict = toolkit.get_action('harvest_job_show')(
            {'ignore_auth': True}, {'id': job.id})
        assert job_dict['full_refresh'] is True
