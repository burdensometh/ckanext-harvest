# -*- coding: utf-8 -*-
"""Tests for the read-only harvest import preview."""

import json

import pytest

from ckan import model
from ckan.plugins import toolkit
from ckantoolkit.tests.helpers import call_action, call_auth

from ckanext.harvest import preview
from ckanext.harvest.harvesters.ckanharvester import CKANHarvester
from ckanext.harvest.model import HarvestObject
from ckanext.harvest.tests.factories import (HarvestJobObj, HarvestObjectObj,
                                             HarvestSourceObj)
from ckanext.harvest.tests.harvesters import mock_ckan
from ckanext.harvest.tests.lib import run_harvest

mock_ckan.serve()

MOCK_URL = 'http://localhost:%s/' % mock_ckan.PORT


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _published_objects(source):
    """Return ``{guid: harvest_object}`` for the current objects of a source."""
    return {
        obj.guid: obj
        for obj in model.Session.query(HarvestObject)
        .filter_by(harvest_source_id=source.id, current=True).all()
    }


def _make_preview_object(dataset, source=None):
    source = source or HarvestSourceObj(url=MOCK_URL, source_type='ckan')
    job = HarvestJobObj(source=source, run=False)
    obj = HarvestObjectObj(
        guid=dataset['id'], content=json.dumps(dataset), job=job)
    obj.current = True
    obj.save()
    return source, obj


def _changes_by_field(result):
    return {change['field']: change for change in result['changes']}


# ---------------------------------------------------------------------------
# Pure helper tests
# ---------------------------------------------------------------------------

class TestConfigNormalisation(object):

    def test_license_defaults_valid(self):
        defaults, errors = preview.normalise_license_defaults(
            {'license_id': 'cc-by', 'override': True})
        assert errors == []
        assert defaults['license_id'] == 'cc-by'

    def test_license_defaults_not_a_dict(self):
        defaults, errors = preview.normalise_license_defaults(['cc-by'])
        assert defaults is None
        assert errors

    def test_license_defaults_needs_a_field(self):
        defaults, errors = preview.normalise_license_defaults({})
        assert errors

    def test_resource_filters_include_exclude(self):
        rules, errors = preview.normalise_resource_filters({
            'include': [{'field': 'format', 'values': ['CSV']}],
            'exclude': [{'field': 'url', 'pattern': r'\.zip$'}],
        })
        assert errors == []
        assert len(rules['include']) == 1
        assert len(rules['exclude']) == 1

    def test_resource_filters_shorthand(self):
        rules, errors = preview.normalise_resource_filters(
            {'include_formats': 'CSV', 'exclude_formats': ['PDF']})
        assert errors == []
        assert len(rules['include']) == 1
        assert len(rules['exclude']) == 1

    def test_resource_filters_unknown_field(self):
        rules, errors = preview.normalise_resource_filters(
            {'include': [{'field': 'license', 'values': ['x']}]})
        assert rules is None
        assert errors

    def test_resource_filters_invalid_pattern(self):
        rules, errors = preview.normalise_resource_filters(
            {'exclude': [{'field': 'url', 'pattern': '('}]})
        assert rules is None
        assert errors

    def test_filter_resources_reports_reasons(self):
        removed = []
        kept = preview.filter_resources(
            [
                {'url': 'http://a/x.csv', 'format': 'CSV'},
                {'url': 'http://a/y.pdf', 'format': 'PDF'},
            ],
            {'include': [], 'exclude': [
                {'field': 'format', 'values': ['csv'], 'pattern': None,
                 'raw': {'field': 'format', 'values': ['csv']}},
            ]},
            removed,
        )
        assert len(kept) == 1
        assert kept[0]['format'] == 'PDF'
        assert len(removed) == 1
        assert removed[0]['resource']['format'] == 'CSV'
        assert 'exclude' in removed[0]['reason']

    def test_dataset_block_reason(self):
        rules, _ = preview.normalise_dataset_filters(
            {'include': [{'field': 'license_id', 'values': ['cc-by']}]})
        assert preview.dataset_block_reason({'license_id': 'cc-by'}, rules) \
            is None
        assert preview.dataset_block_reason({'license_id': 'ogl'}, rules)

    def test_diff_ignores_absent_fields(self):
        diff = preview.diff_packages({'title': 'X'}, {'title': 'X',
                                                      'private': False})
        assert diff['has_changes'] is False

    def test_diff_detects_extras_and_tags(self):
        proposed = {
            'title': 'X',
            'tags': [{'name': 'a'}, {'name': 'b'}],
            'extras': [{'key': 'encoding', 'value': 'utf8'}],
        }
        published = {
            'title': 'X',
            'tags': [{'name': 'a'}],
            'extras': [{'key': 'encoding', 'value': 'latin1'}],
        }
        diff = preview.diff_packages(proposed, published)
        fields = {change['field'] for change in diff['changes']}
        assert 'tags' in fields
        assert 'extras:encoding' in fields

    def test_apply_license_defaults_ignores_url_write(self):
        package = {}
        notes = []
        preview.apply_license_defaults(
            package, {'license_id': 'cc-by', 'license_url': 'http://x'},
            notes=notes)
        assert package == {'license_id': 'cc-by'}
        assert any('license_url' in note for note in notes)

    def test_validate_config_accepts_new_options(self):
        config = json.dumps({
            'license_defaults': {'license_id': 'cc-by'},
            'resource_filters': {'exclude_formats': ['PDF']},
            'dataset_filters': {'include': [
                {'field': 'license_id', 'values': ['cc-by']}]},
        })
        assert CKANHarvester().validate_config(config) == config

    def test_validate_config_rejects_bad_license_defaults(self):
        with pytest.raises(ValueError):
            CKANHarvester().validate_config(
                json.dumps({'license_defaults': {'override': True}}))

    def test_validate_config_rejects_bad_resource_filters(self):
        with pytest.raises(ValueError):
            CKANHarvester().validate_config(
                json.dumps({'resource_filters': {
                    'include': [{'field': 'nope', 'values': ['x']}]}}))


# ---------------------------------------------------------------------------
# Harvester preview
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures('with_plugins', 'clean_db', 'clean_index')
class TestCkanHarvesterPreview(object):

    def _harvested_source(self):
        run_harvest(url=MOCK_URL, harvester=CKANHarvester())
        return HarvestSourceObj(url=MOCK_URL)

    def test_preview_unchanged_with_current_config(self):
        source = self._harvested_source()
        obj = _published_objects(source)[mock_ckan.DATASETS[0]['id']]

        result = CKANHarvester().preview_harvest_object(obj)

        assert result['status'] == preview.STATUS_UNCHANGED
        assert result['changes'] == []
        assert result['reason'] == 'no changes compared to the published record'

    def test_preview_license_default_added(self):
        source = self._harvested_source()
        obj = _published_objects(source)[mock_ckan.DATASETS[0]['id']]

        config = json.dumps({
            'license_defaults': {'license_id': 'cc-by'}
        })
        result = CKANHarvester().preview_harvest_object(obj, config)

        assert result['status'] == preview.STATUS_UPDATED
        change = _changes_by_field(result)['license_id']
        assert change['new'] == 'cc-by'
        assert change['old'] in (None, '', 'notspecified')

    def test_preview_license_default_does_not_override_by_default(self):
        source = self._harvested_source()
        obj = _published_objects(source)[mock_ckan.DATASETS[1]['id']]

        config = json.dumps({
            'license_defaults': {'license_id': 'cc-by'}
        })
        result = CKANHarvester().preview_harvest_object(obj, config)

        # The second remote dataset already has a license, so nothing changes.
        assert 'license_id' not in _changes_by_field(result)

    def test_preview_license_default_override(self):
        source = self._harvested_source()
        obj = _published_objects(source)[mock_ckan.DATASETS[1]['id']]

        config = json.dumps({
            'license_defaults': {'license_id': 'cc-by', 'override': True}
        })
        result = CKANHarvester().preview_harvest_object(obj, config)

        change = _changes_by_field(result)['license_id']
        assert change['old'] == 'uk-ogl'
        assert change['new'] == 'cc-by'

    def test_preview_resource_filter_reports_removed_resources(self):
        source = self._harvested_source()
        obj = _published_objects(source)[mock_ckan.DATASETS[1]['id']]

        config = json.dumps({
            'resource_filters': {'exclude_formats': ['CSV']}
        })
        result = CKANHarvester().preview_harvest_object(obj, config)

        assert result['status'] == preview.STATUS_UPDATED
        assert len(result['removed_resources']) == 1
        removed = result['removed_resources'][0]
        assert removed['resource']['format'] == 'CSV'
        assert 'exclude' in removed['reason']
        assert result['resources']['removed'][0]['format'] == 'CSV'

    def test_preview_include_resource_filter(self):
        source = self._harvested_source()
        obj = _published_objects(source)[mock_ckan.DATASETS[1]['id']]

        config = json.dumps({
            'resource_filters': {'include_formats': ['PDF']}
        })
        result = CKANHarvester().preview_harvest_object(obj, config)

        assert len(result['removed_resources']) == 1
        assert 'include' in result['removed_resources'][0]['reason']

    def test_preview_dataset_filter_blocks(self):
        source = self._harvested_source()
        obj = _published_objects(source)[mock_ckan.DATASETS[0]['id']]

        config = json.dumps({
            'dataset_filters': {
                'include': [{'field': 'name', 'values': ['not-this-one']}]
            }
        })
        result = CKANHarvester().preview_harvest_object(obj, config)

        assert result['status'] == preview.STATUS_BLOCKED
        assert 'include filter' in result['reason']
        assert result['changes'] == []

    def test_preview_organization_filter_blocks(self):
        source = self._harvested_source()
        obj = _published_objects(source)[mock_ckan.DATASETS[0]['id']]

        config = json.dumps({
            'organizations_filter_include': ['some-other-org']
        })
        result = CKANHarvester().preview_harvest_object(obj, config)

        assert result['status'] == preview.STATUS_BLOCKED
        assert 'organizations_filter_include' in result['reason']

    def test_gather_applies_dataset_filters(self):
        config = json.dumps({
            'dataset_filters': {
                'include': [{'field': 'name', 'values': ['dataset1']}]
            }
        })
        source = HarvestSourceObj(url=MOCK_URL, source_type='ckan',
                                  config=config)
        job = HarvestJobObj(source=source, run=False)

        object_ids = CKANHarvester().gather_stage(job)

        guids = sorted(
            model.Session.query(HarvestObject.guid)
            .filter(HarvestObject.id.in_(object_ids)).all())
        assert guids == [('dataset1-id',)]

    def test_import_applies_new_options(self):
        config = json.dumps({
            'license_defaults': {'license_id': 'cc-by', 'override': True},
            'resource_filters': {'exclude_formats': ['CSV']},
        })
        source = HarvestSourceObj(url=MOCK_URL, source_type='ckan',
                                  config=config)
        job = HarvestJobObj(source=source, run=False)
        dataset = dict(mock_ckan.DATASETS[1])
        obj = HarvestObjectObj(
            guid=dataset['id'], content=json.dumps(dataset), job=job)

        CKANHarvester().import_stage(obj)
        model.Session.expire_all()
        obj = HarvestObject.get(obj.id)

        assert obj.state != 'ERROR', obj.errors
        package = call_action('package_show', {'ignore_auth': True},
                              id=obj.package_id)
        assert package['license_id'] == 'cc-by'
        assert package['num_resources'] == 0

    def test_preview_matches_real_import(self):
        config = json.dumps({
            'license_defaults': {'license_id': 'cc-by', 'override': True},
            'resource_filters': {'exclude_formats': ['CSV']},
        })
        # The published record is imported with the base configuration.
        source = HarvestSourceObj(url=MOCK_URL, source_type='ckan')
        job = HarvestJobObj(source=source, run=False)
        dataset = dict(mock_ckan.DATASETS[1])

        base_obj = HarvestObjectObj(
            guid=dataset['id'], content=json.dumps(dataset), job=job)
        CKANHarvester().import_stage(base_obj)
        model.Session.expire_all()
        base_obj = HarvestObject.get(base_obj.id)
        assert base_obj.package_id

        # Preview with the pending configuration, which is not yet saved on
        # the source.
        preview_obj = HarvestObjectObj(
            guid=dataset['id'], content=json.dumps(dataset), job=job)
        preview_obj.current = True
        preview_obj.package_id = base_obj.package_id
        preview_obj.save()
        result = CKANHarvester().preview_harvest_object(preview_obj, config)

        fields = {change['field'] for change in result['changes']}
        assert 'license_id' in fields
        assert result['resources']['removed'][0]['format'] == 'CSV'

    def test_preview_skips_harvest_sources(self):
        source, obj = _make_preview_object({
            'id': 'remote-harvest-source',
            'name': 'remote-harvest-source',
            'type': 'harvest',
        })

        result = CKANHarvester().preview_harvest_object(obj)

        assert result['status'] == preview.STATUS_SKIPPED
        assert 'harvest source' in result['reason']

    def test_preview_reports_creation_for_unpublished_record(self):
        dataset = dict(mock_ckan.DATASETS[0])
        dataset['id'] = 'not-published-id'
        dataset['name'] = 'not-published'
        source, obj = _make_preview_object(dataset)

        result = CKANHarvester().preview_harvest_object(obj)

        assert result['status'] == preview.STATUS_CREATED
        assert result['package_name'] == 'not-published'

    def test_preview_does_not_change_publication_state(self):
        source = self._harvested_source()
        before = _published_objects(source)
        before_state = {
            guid: (obj.id, obj.package_id, obj.current, obj.report_status)
            for guid, obj in before.items()
        }
        package_count = model.Session.query(model.Package).count()

        obj = before[mock_ckan.DATASETS[0]['id']]
        config = json.dumps({
            'license_defaults': {'license_id': 'cc-by', 'override': True},
            'resource_filters': {'exclude_formats': ['CSV']},
        })
        CKANHarvester().preview_harvest_object(obj, config)

        model.Session.expire_all()
        after = _published_objects(source)
        after_state = {
            guid: (obj.id, obj.package_id, obj.current, obj.report_status)
            for guid, obj in after.items()
        }
        assert after_state == before_state
        assert model.Session.query(model.Package).count() == package_count

    def test_preview_does_not_modify_package(self):
        source = self._harvested_source()
        obj = _published_objects(source)[mock_ckan.DATASETS[1]['id']]
        package_id = obj.package_id

        before = call_action('package_show', {'ignore_auth': True},
                             id=package_id)
        config = json.dumps({
            'license_defaults': {'license_id': 'cc-by', 'override': True},
            'resource_filters': {'exclude_formats': ['CSV']},
        })
        CKANHarvester().preview_harvest_object(obj, config)
        after = call_action('package_show', {'ignore_auth': True},
                            id=package_id)

        assert before['license_id'] == after['license_id']
        assert len(before['resources']) == len(after['resources'])

    def test_preview_metadata_modified_would_skip(self):
        source = self._harvested_source()
        # The second remote dataset carries a metadata_modified date.
        obj = _published_objects(source)[mock_ckan.DATASETS[1]['id']]

        config = json.dumps({
            'license_defaults': {'license_id': 'cc-by', 'override': True}
        })
        result = CKANHarvester().preview_harvest_object(obj, config)

        # The remote record's metadata_modified is older than the published
        # one, so a normal import would skip it before applying any changes.
        assert result['metadata_modified_would_skip'] is True
        assert any('not modified' in note for note in result['notes'])

    def test_preview_does_not_create_remote_organizations(self):
        dataset = dict(mock_ckan.DATASETS[1])
        source, obj = _make_preview_object(dataset)

        config = json.dumps({'remote_orgs': 'create'})
        result = CKANHarvester().preview_harvest_object(obj, config)

        with pytest.raises(toolkit.ObjectNotFound):
            call_action('organization_show', {'ignore_auth': True},
                        id=dataset['owner_org'])
        assert any('would be created' in note for note in result['notes'])

    def test_preview_does_not_create_site_user(self):
        source, obj = _make_preview_object(mock_ckan.DATASETS[0])
        user_count = model.Session.query(model.User).count()

        CKANHarvester().preview_harvest_object(obj, '{}')

        assert model.Session.query(model.User).count() == user_count

    def test_preview_invalid_config_reports_error(self):
        source = self._harvested_source()
        obj = _published_objects(source)[mock_ckan.DATASETS[0]['id']]

        result = CKANHarvester().preview_harvest_object(obj, '{not json')

        assert result['status'] == preview.STATUS_ERROR
        assert 'valid JSON' in result['reason']


# ---------------------------------------------------------------------------
# Action
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures('with_plugins', 'clean_db', 'clean_index')
class TestHarvestObjectsPreviewAction(object):

    def test_requires_an_identifier(self):
        with pytest.raises(toolkit.ValidationError):
            call_action('harvest_objects_preview')

    def test_summary_and_results(self):
        run_harvest(url=MOCK_URL, harvester=CKANHarvester())
        source = call_action('harvest_source_show', {'ignore_auth': True},
                             url=MOCK_URL)

        config = json.dumps({
            'license_defaults': {'license_id': 'cc-by', 'override': True}
        })
        report = call_action(
            'harvest_objects_preview', {'ignore_auth': True},
            source_id=source['id'], config=config)

        assert report['summary']['total'] == len(report['results'])
        assert report['summary']['updated'] == len(mock_ckan.DATASETS)
        assert report['summary']['blocked'] == 0
        assert report['options']['config_provided'] is True

    def test_include_unchanged_false(self):
        run_harvest(url=MOCK_URL, harvester=CKANHarvester())
        source = call_action('harvest_source_show', {'ignore_auth': True},
                             url=MOCK_URL)

        report = call_action(
            'harvest_objects_preview', {'ignore_auth': True},
            source_id=source['id'], include_unchanged=False)

        assert report['summary']['total'] == len(mock_ckan.DATASETS)
        assert report['results'] == []

    def test_invalid_config_json(self):
        run_harvest(url=MOCK_URL, harvester=CKANHarvester())
        source = call_action('harvest_source_show', {'ignore_auth': True},
                             url=MOCK_URL)

        with pytest.raises(toolkit.ValidationError):
            call_action('harvest_objects_preview', {'ignore_auth': True},
                        source_id=source['id'], config='{not json')

    def test_only_sysadmins(self):
        # Create the user through the model to avoid the uploader needing a
        # writable storage directory in this environment.
        user = model.User(name='plain-user', fullname='Plain User')
        user.password = 'secret1234'
        user.save()
        source, obj = _make_preview_object(mock_ckan.DATASETS[0])

        with pytest.raises(toolkit.NotAuthorized):
            call_auth('harvest_objects_preview',
                      {'user': user.name, 'model': model},
                      harvest_object_id=obj.id)

    def test_anonymous_not_authorized(self):
        run_harvest(url=MOCK_URL, harvester=CKANHarvester())
        source = call_action('harvest_source_show', {'ignore_auth': True},
                             url=MOCK_URL)

        with pytest.raises(toolkit.NotAuthorized):
            call_action('harvest_objects_preview',
                        {'user': 'nobody', 'ignore_auth': False},
                        source_id=source['id'])


# ---------------------------------------------------------------------------
# CLI report formatting
# ---------------------------------------------------------------------------

class TestPreviewReportFormatting(object):

    def test_report_contains_summary_and_reasons(self):
        report = {
            'summary': {
                'total': 2, 'created': 0, 'updated': 1, 'unchanged': 0,
                'skipped': 0, 'blocked': 1, 'error': 0,
            },
            'results': [
                {
                    'guid': 'g-updated',
                    'harvest_object_id': 'obj-1',
                    'status': 'updated',
                    'package_name': 'dataset-one',
                    'package_id': 'pkg-1',
                    'reason': 'dataset would be updated',
                    'notes': ['license default applied'],
                    'changes': [{'field': 'license_id', 'old': None,
                                 'new': 'cc-by'}],
                    'resources': {'added': [], 'removed': [], 'changed': []},
                    'removed_resources': [],
                },
                {
                    'guid': 'g-blocked',
                    'harvest_object_id': 'obj-2',
                    'status': 'blocked',
                    'reason': 'dataset did not match any include filter',
                    'notes': [],
                    'changes': [],
                    'resources': {'added': [], 'removed': [], 'changed': []},
                    'removed_resources': [
                        {'resource': {'url': 'http://x/y.csv',
                                      'format': 'CSV'},
                         'reason': 'resource matched an exclude filter'},
                    ],
                },
            ],
        }

        from ckanext.harvest.utils import format_preview_report
        output = format_preview_report(report)

        assert 'Objects considered: 2' in output
        assert '[UPDATED] g-updated' in output
        assert "changed license_id" in output
        assert '[BLOCKED] g-blocked' in output
        assert 'resource filtered out' in output
