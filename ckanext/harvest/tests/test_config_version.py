import json

import pytest

from ckan import model
from ckantoolkit.tests import factories as ckan_factories, helpers
from ckantoolkit import ValidationError

import ckanext.harvest.model as harvest_model
from ckanext.harvest.model import HarvestConfigVersion
from ckanext.harvest.tests import factories
from ckanext.harvest.tests.test_action import MockHarvesterForActionTests


CONFIG_OLD = json.dumps({'custom_option': ['old']})
CONFIG_NEW = json.dumps({'custom_option': ['new']})


def _make_source(**kwargs):
    kwargs.setdefault('source_type', 'test-for-action')
    return factories.HarvestSourceObj(**kwargs)


def _finish_job(job):
    job.status = 'Finished'
    job.save()


@pytest.fixture
def writable_storage(ckan_config, tmp_path):
    ckan_config['ckan.storage_path'] = str(tmp_path)


def _record_import(recorded):
    '''Returns an import_stage implementation that records which config
    the harvest object is being processed with.'''
    def import_stage(harvester_self, harvest_object):
        recorded.append(harvest_object.job.get_config())
        return True
    return import_stage


@pytest.mark.usefixtures('with_plugins', 'clean_db', 'clean_queues')
@pytest.mark.ckan_config('ckan.plugins', 'harvest test_action_harvester')
class TestConfigVersionPinning(object):

    def test_job_create_pins_config_version(self, writable_storage):
        org = ckan_factories.Organization()
        source = _make_source(config=CONFIG_OLD, owner_org=org['id'])

        job = factories.HarvestJobObj(source=source)

        assert job.config_version is not None
        assert job.config_version.version == 1
        assert job.config_version.config == CONFIG_OLD
        assert job.config_version.owner_org == org['id']
        assert job.get_config() == CONFIG_OLD
        assert job.get_owner_org() == org['id']

    def test_job_show_includes_config_version(self):
        source = _make_source(config=CONFIG_OLD)
        job = factories.HarvestJobObj(source=source)

        job_dict = helpers.call_action('harvest_job_show', id=job.id)

        assert job_dict['config_version']['version'] == 1
        assert job_dict['config_version']['config'] == CONFIG_OLD

    def test_jobs_share_version_when_config_unchanged(self):
        source = _make_source(config=CONFIG_OLD)
        job1 = factories.HarvestJobObj(source=source)
        _finish_job(job1)

        job2 = factories.HarvestJobObj(source=source)

        assert job2.config_version.id == job1.config_version.id
        assert model.Session.query(HarvestConfigVersion).count() == 1

    def test_new_version_when_config_changes(self):
        source = _make_source(config=CONFIG_OLD)
        job1 = factories.HarvestJobObj(source=source)
        _finish_job(job1)

        helpers.call_action(
            'harvest_source_patch', id=source.id, config=CONFIG_NEW)
        job2 = factories.HarvestJobObj(source=source)

        assert job2.config_version.version == 2
        assert job2.config_version.config == CONFIG_NEW
        # The original job keeps the config confirmed when it was created
        assert job1.get_config() == CONFIG_OLD
        assert job2.get_config() == CONFIG_NEW

    def test_new_version_when_owner_org_changes(self, writable_storage):
        org1 = ckan_factories.Organization()
        org2 = ckan_factories.Organization()
        source = _make_source(config=CONFIG_OLD, owner_org=org1['id'])
        job1 = factories.HarvestJobObj(source=source)
        _finish_job(job1)

        helpers.call_action(
            'harvest_source_patch', id=source.id, owner_org=org2['id'])
        job2 = factories.HarvestJobObj(source=source)

        assert job2.config_version.version == 2
        assert job2.config_version.owner_org == org2['id']
        assert job1.get_owner_org() == org1['id']
        assert job2.get_owner_org() == org2['id']

    def test_legacy_job_falls_back_to_live_config(self, writable_storage):
        org = ckan_factories.Organization()
        source = _make_source(config=CONFIG_OLD, owner_org=org['id'])
        job = factories.HarvestJobObj(source=source)
        job_id = job.id

        # Simulate a job created before config versioning existed
        job.config_version_id = None
        model.Session.commit()
        model.Session.expire_all()

        helpers.call_action(
            'harvest_source_patch', id=source.id, config=CONFIG_NEW)

        job = harvest_model.HarvestJob.get(job_id)
        assert job.get_config_version() is None
        assert job.get_config() == CONFIG_NEW
        assert job.get_owner_org() == org['id']

    def test_config_version_list(self):
        source = _make_source(config=CONFIG_OLD)
        job1 = factories.HarvestJobObj(source=source)
        _finish_job(job1)
        helpers.call_action(
            'harvest_source_patch', id=source.id, config=CONFIG_NEW)
        factories.HarvestJobObj(source=source)

        versions = helpers.call_action(
            'harvest_config_version_list', source_id=source.id)

        assert [v['version'] for v in versions] == [2, 1]
        assert versions[0]['config'] == CONFIG_NEW
        assert versions[1]['config'] == CONFIG_OLD

    def test_job_report_includes_config_version(self):
        source = _make_source(config=CONFIG_OLD)
        job = factories.HarvestJobObj(source=source)

        report = helpers.call_action('harvest_job_report', id=job.id)

        assert report['config_version']['version'] == 1
        assert report['config_version']['config'] == CONFIG_OLD


@pytest.mark.usefixtures('with_plugins', 'clean_db', 'clean_queues')
@pytest.mark.ckan_config('ckan.plugins', 'harvest test_action_harvester')
class TestObjectsImportConfigVersion(object):

    def _make_object(self, config=CONFIG_OLD):
        source = _make_source(config=config)
        job = factories.HarvestJobObj(source=source)
        dataset = ckan_factories.Dataset()
        obj = factories.HarvestObjectObj(job=job, package_id=dataset['id'])
        obj.current = True
        obj.save()
        return source, job, obj

    def test_import_uses_pinned_config_by_default(self, monkeypatch):
        source, job, obj = self._make_object()
        # The admin changes the source configuration while the object is
        # still waiting to be imported
        helpers.call_action(
            'harvest_source_patch', id=source.id, config=CONFIG_NEW)

        recorded = []
        monkeypatch.setattr(
            MockHarvesterForActionTests, 'import_stage',
            _record_import(recorded))

        count = helpers.call_action(
            'harvest_objects_import', source_id=source.id)

        assert count == 1
        assert recorded == [CONFIG_OLD]

    def test_import_with_current_config(self, monkeypatch):
        source, job, obj = self._make_object()
        helpers.call_action(
            'harvest_source_patch', id=source.id, config=CONFIG_NEW)

        recorded = []
        monkeypatch.setattr(
            MockHarvesterForActionTests, 'import_stage',
            _record_import(recorded))

        count = helpers.call_action(
            'harvest_objects_import', source_id=source.id,
            config_version='current')

        assert count == 1
        assert recorded == [CONFIG_NEW]
        # The current config was confirmed as a new traceable version...
        versions = model.Session.query(HarvestConfigVersion) \
            .order_by(HarvestConfigVersion.version).all()
        assert [v.version for v in versions] == [1, 2]
        assert versions[1].config == CONFIG_NEW
        # ...while the job keeps its originally pinned version
        assert job.config_version.version == 1

    def test_import_with_current_config_reuses_matching_version(
            self, monkeypatch):
        source, job, obj = self._make_object()
        helpers.call_action(
            'harvest_source_patch', id=source.id, config=CONFIG_NEW)

        recorded = []
        monkeypatch.setattr(
            MockHarvesterForActionTests, 'import_stage',
            _record_import(recorded))

        helpers.call_action(
            'harvest_objects_import', source_id=source.id,
            config_version='current')
        helpers.call_action(
            'harvest_objects_import', source_id=source.id,
            config_version='current')

        # The second replay did not create a duplicate version
        assert model.Session.query(HarvestConfigVersion).count() == 2
        assert recorded == [CONFIG_NEW, CONFIG_NEW]

    def test_import_with_invalid_config_version(self):
        source, job, obj = self._make_object()

        with pytest.raises(ValidationError):
            helpers.call_action(
                'harvest_objects_import', source_id=source.id,
                config_version='bogus')
