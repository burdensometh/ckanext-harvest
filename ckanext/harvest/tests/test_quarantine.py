# -*- coding: utf-8 -*-
"""Tests for quarantining records that do not comply with source field rules
and replaying them after the rules are corrected."""

import copy
import json

import pytest

from ckan import model
from ckan.plugins import toolkit
from ckantoolkit.tests import factories as ckan_factories
from ckantoolkit.tests.helpers import call_action

from ckanext.harvest.harvesters.ckanharvester import CKANHarvester
from ckanext.harvest.logic.quarantine import list_entries
from ckanext.harvest.model import HarvestObject, HarvestQuarantine, HarvestSource
from ckanext.harvest.tests.factories import HarvestSourceObj
from ckanext.harvest.tests.lib import run_harvest

from ckanext.harvest.tests.harvesters import mock_ckan

# The mock server is started by test_ckanharvester; calling ``serve`` here as
# well makes this module runnable on its own (it is a no-op if the server is
# already up).
mock_ckan.serve()

RULES_CONFIG = {
    'field_rules': {
        'required': ['title'],
        'allowed': {'license_id': ['cc-by', 'uk-ogl']},
    }
}


def _config(**extra):
    config = copy.deepcopy(RULES_CONFIG)
    config.update(extra)
    return json.dumps(config)


@pytest.mark.usefixtures('with_plugins', 'clean_db', 'clean_index')
class TestFieldRuleQuarantine(object):

    def _dataset_count(self):
        return model.Session.query(model.Package).filter(
            model.Package.type == 'dataset').count()

    def test_compliant_records_are_imported(self):
        datasets = copy.deepcopy(mock_ckan.DATASETS)
        datasets[0]['license_id'] = 'cc-by'
        with _patched_datasets(datasets):
            results = run_harvest(
                url='http://localhost:%s/' % mock_ckan.PORT,
                harvester=CKANHarvester(),
                config=_config())

        assert results['dataset1-id']['report_status'] == 'added'
        # The second mock dataset has uk-ogl, which is allowed.
        assert results[mock_ckan.DATASETS[1]['id']]['report_status'] == 'added'
        assert list_entries() == []

    def test_missing_license_is_quarantined(self):
        # ``allowed`` means the agreed licence must be present: an entry that
        # no longer provides a licence is set aside.
        results = run_harvest(
            url='http://localhost:%s/' % mock_ckan.PORT,
            harvester=CKANHarvester(),
            config=_config())

        # dataset1 has no licence, so it is quarantined; dataset2 is fine.
        assert results['dataset1-id']['report_status'] == 'quarantined'
        assert results[mock_ckan.DATASETS[1]['id']]['report_status'] == 'added'

        entry = list_entries()[0]
        assert entry.guid == 'dataset1-id'
        assert json.loads(entry.violations) == [{
            'field': 'license_id',
            'reason': 'missing',
            'allowed': ['cc-by', 'uk-ogl'],
        }]

    def test_non_compliant_record_is_quarantined_and_good_ones_imported(self):
        # A licence that is not in the agreed list is rejected.
        config = _config()
        datasets = copy.deepcopy(mock_ckan.DATASETS)
        datasets[0]['license_id'] = 'cc-by'
        datasets[1]['license_id'] = 'not-agreed'
        datasets[1]['license_title'] = 'Not agreed'

        with _patched_datasets(datasets):
            results = run_harvest(
                url='http://localhost:%s/' % mock_ckan.PORT,
                harvester=CKANHarvester(),
                config=config)

        # The good record is imported as usual.
        good = results['dataset1-id']
        assert good['state'] == 'COMPLETE'
        assert good['report_status'] == 'added'
        assert good['errors'] == []

        # The bad record is not an error: it is set aside.
        bad = results[mock_ckan.DATASETS[1]['id']]
        assert bad['state'] == 'COMPLETE'
        assert bad['report_status'] == 'quarantined'
        assert bad['errors'] == []

        entries = list_entries()
        assert len(entries) == 1
        entry = entries[0]
        assert entry.guid == mock_ckan.DATASETS[1]['id']
        assert entry.status == 'pending'

        violations = json.loads(entry.violations)
        assert violations == [{
            'field': 'license_id',
            'reason': 'not_allowed',
            'value': 'not-agreed',
            'allowed': ['cc-by', 'uk-ogl'],
        }]
        # The original payload is retained for the administrator.
        original = json.loads(entry.content)
        assert original['license_id'] == 'not-agreed'

        # Only the good dataset exists locally.
        assert self._dataset_count() == 1

    def test_missing_title_is_quarantined(self):
        datasets = copy.deepcopy(mock_ckan.DATASETS)
        datasets[1].pop('title', None)
        datasets[1]['license_id'] = 'cc-by'

        with _patched_datasets(datasets):
            results = run_harvest(
                url='http://localhost:%s/' % mock_ckan.PORT,
                harvester=CKANHarvester(),
                config=_config())

        bad = results[mock_ckan.DATASETS[1]['id']]
        assert bad['report_status'] == 'quarantined'

        entry = list_entries()[0]
        violations = json.loads(entry.violations)
        assert {'field': 'title', 'reason': 'missing'} in violations

    def test_quarantined_record_does_not_overwrite_existing_dataset(self):
        url = 'http://localhost:%s/' % mock_ckan.PORT
        # First harvest imports both records without rules.
        run_harvest(url=url, harvester=CKANHarvester())
        local = call_action('package_show', {}, id=mock_ckan.DATASETS[1]['id'])
        original_title = local['title']

        # Add rules to the existing source so the second record is rejected
        # on the next harvest.  ``run_harvest`` reuses the source for the
        # same URL, so look it up the same way.
        source_dict = call_action('harvest_source_show', {}, url=url)
        source = HarvestSource.get(source_dict['id'])
        source.config = _config()
        source.save()

        datasets = copy.deepcopy(mock_ckan.DATASETS)
        datasets[1]['title'] = 'Changed title'
        datasets[1]['license_id'] = 'not-agreed'
        datasets[1]['metadata_modified'] = '2050-05-09T22:00:01.486366'

        with _patched_datasets(datasets):
            run_harvest(url=url, harvester=CKANHarvester())

        # The quarantined record left the local dataset untouched.
        local = call_action('package_show', {}, id=mock_ckan.DATASETS[1]['id'])
        assert local['title'] == original_title

        assert len(list_entries()) == 1

    def test_quarantine_entry_is_updated_not_duplicated(self):
        datasets = copy.deepcopy(mock_ckan.DATASETS)
        datasets[0]['license_id'] = 'cc-by'
        datasets[1]['license_id'] = 'not-agreed'

        with _patched_datasets(datasets):
            run_harvest(
                url='http://localhost:%s/' % mock_ckan.PORT,
                harvester=CKANHarvester(),
                config=_config())
            datasets[1]['metadata_modified'] = '2050-05-09T22:00:01.486366'
            with _patched_datasets(datasets):
                run_harvest(
                    url='http://localhost:%s/' % mock_ckan.PORT,
                    harvester=CKANHarvester(),
                    config=_config())

        assert len(list_entries()) == 1

    def test_job_stats_include_quarantined_count(self):
        datasets = copy.deepcopy(mock_ckan.DATASETS)
        datasets[0]['license_id'] = 'cc-by'
        datasets[1]['license_id'] = 'not-agreed'

        with _patched_datasets(datasets):
            results = run_harvest(
                url='http://localhost:%s/' % mock_ckan.PORT,
                harvester=CKANHarvester(),
                config=_config())

        job = HarvestObject.get(results['dataset1-id']['obj_id'])
        job_dict = call_action('harvest_job_show',
                               {'ignore_auth': True}, id=job.harvest_job_id)
        assert job_dict['stats']['quarantined'] == 1
        assert job_dict['stats']['added'] == 1


@pytest.mark.usefixtures('with_plugins', 'clean_db', 'clean_index')
class TestQuarantineReplay(object):

    def _get_source(self):
        entries = list_entries()
        assert entries
        return entries[0].harvest_source_id

    def _quarantine_one(self):
        datasets = copy.deepcopy(mock_ckan.DATASETS)
        datasets[0]['license_id'] = 'cc-by'
        datasets[1]['license_id'] = 'not-agreed'

        with _patched_datasets(datasets):
            run_harvest(
                url='http://localhost:%s/' % mock_ckan.PORT,
                harvester=CKANHarvester(),
                config=_config())
        return datasets

    def test_replay_after_rules_corrected_imports_record(self):
        self._quarantine_one()
        source_id = self._get_source()

        # Administrator corrects the rules to accept the new licence.
        fixed_config = json.dumps({
            'field_rules': {
                'required': ['title'],
                'allowed': {'license_id': ['cc-by', 'uk-ogl', 'not-agreed']},
            }
        })
        source = HarvestSource.get(source_id)
        source.config = fixed_config
        source.save()

        results = call_action('harvest_quarantine_replay',
                              {'ignore_auth': True},
                              source_id=source_id)
        assert len(results) == 1
        assert results[0]['status'] == 'replayed'

        # The replayed record now exists, and the good one was not changed.
        local = call_action('package_show', {}, id=mock_ckan.DATASETS[1]['id'])
        assert local['license_id'] == 'not-agreed'
        assert call_action('package_show', {}, id='dataset1-id')

        entry = HarvestQuarantine.get(results[0]['id'])
        assert entry.status == 'replayed'

    def test_replay_without_correcting_rules_keeps_entry_pending(self):
        self._quarantine_one()
        source_id = self._get_source()

        results = call_action('harvest_quarantine_replay',
                              {'ignore_auth': True},
                              source_id=source_id)

        assert results[0]['status'] == 'pending'
        assert results[0]['violations']
        entry = HarvestQuarantine.get(results[0]['id'])
        assert entry.status == 'pending'
        assert model.Package.get(mock_ckan.DATASETS[1]['id']) is None

    def test_replay_single_entry(self):
        self._quarantine_one()
        entry = list_entries()[0]

        fixed_config = json.dumps({
            'field_rules': {'required': ['title']}
        })
        source = HarvestSource.get(entry.harvest_source_id)
        source.config = fixed_config
        source.save()

        results = call_action('harvest_quarantine_replay',
                              {'ignore_auth': True},
                              id=entry.id)
        assert results[0]['status'] == 'replayed'
        assert model.Package.get(mock_ckan.DATASETS[1]['id']) is not None

    def test_replay_requires_a_target(self):
        with pytest.raises(toolkit.ValidationError):
            call_action('harvest_quarantine_replay', {'ignore_auth': True})

    def test_list_is_restricted_to_source_admins(self):
        member = ckan_factories.User()
        member['capacity'] = 'admin'
        org = ckan_factories.Organization(users=[member])

        source = HarvestSourceObj(owner_org=org['id'])
        source_obj = source

        harvest_object = HarvestObject(
            guid='restricted-guid',
            harvest_source_id=source_obj.id,
            content=json.dumps({'id': 'restricted-guid', 'title': 'x'}),
        )
        from ckanext.harvest.logic.quarantine import quarantine_object
        quarantine_object(harvest_object, [{'field': 'title', 'reason': 'missing'}])

        # A regular user cannot inspect quarantined payloads.
        user = ckan_factories.User()
        with pytest.raises(toolkit.NotAuthorized):
            toolkit.check_access(
                'harvest_quarantine_list',
                {'model': model, 'user': user['name']},
                {'source_id': source_obj.id})

        # A member of the owning organization can.
        assert toolkit.check_access(
            'harvest_quarantine_list',
            {'model': model, 'user': member['name']},
            {'source_id': source_obj.id})
        entries = call_action('harvest_quarantine_list',
                              {'user': member['name'], 'ignore_auth': True},
                              source_id=source_obj.id)
        assert len(entries) == 1

    def test_list_can_filter_by_source_and_status(self):
        self._quarantine_one()
        source_id = self._get_source()

        entries = call_action('harvest_quarantine_list',
                              {'ignore_auth': True},
                              source_id=source_id, status='pending')
        assert len(entries) == 1
        assert entries[0]['violations'][0]['field'] == 'license_id'

        entries = call_action('harvest_quarantine_list',
                              {'ignore_auth': True},
                              source_id=source_id, status='replayed')
        assert entries == []

    def test_clearing_source_removes_quarantine_entries(self):
        self._quarantine_one()
        source_id = self._get_source()

        call_action('harvest_source_clear',
                    {'ignore_auth': True}, id=source_id)

        assert list_entries(source_id=source_id) == []

    def test_clearing_job_history_removes_quarantine_entries(self):
        self._quarantine_one()
        source_id = self._get_source()

        call_action('harvest_source_job_history_clear',
                    {'ignore_auth': True}, id=source_id)

        assert list_entries(source_id=source_id) == []


class _patched_datasets(object):
    """Context manager patching the mock server datasets."""

    def __init__(self, datasets):
        self.datasets = datasets
        self._patch = None

    def __enter__(self):
        from unittest.mock import patch
        self._patch = patch(
            'ckanext.harvest.tests.harvesters.mock_ckan.DATASETS',
            self.datasets)
        return self._patch.start()

    def __exit__(self, *exc_info):
        self._patch.stop()
        return False
