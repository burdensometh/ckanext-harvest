import json

import pytest

from ckan import model
from ckan.plugins import toolkit
from ckantoolkit.tests.factories import Organization

import ckanext.harvest.model as harvest_model
from ckanext.harvest.harvesters.base import (
    HarvesterBase,
    MERGE_BASE_EXTRA,
    MERGE_CONFLICTS_EXTRA,
)
from ckanext.harvest.tests.factories import (HarvestSourceObj, HarvestJobObj,
                                             HarvestObjectObj)


def _upstream_dict(**overrides):
    data = {
        'id': 'merge-dataset-id',
        'name': 'merge-dataset',
        'title': 'Merge dataset',
        'notes': 'Upstream description',
        'license_id': 'cc-by',
        'url': 'http://example.com/dataset',
        'metadata_modified': '2020-01-01T00:00:00',
        'tags': [{'name': 'alpha'}, {'name': 'beta'}],
        'extras': [{'key': 'owner', 'value': 'upstream'}],
        'resources': [{
            'id': 'f156019d-ea88-46a6-8fa3-3d12582e2161',
            'url': 'http://example.com/data.csv',
            'format': 'CSV',
            'name': 'data',
        }],
    }
    data.update(overrides)
    return data


@pytest.mark.usefixtures('with_plugins', 'clean_db', 'clean_index')
class TestThreeWayMerge(object):

    @pytest.fixture(autouse=True)
    def _setup(self, with_plugins, clean_db, clean_index):
        self.harvester = HarvesterBase()
        self.org = Organization()
        source = HarvestSourceObj(url='http://example.com/source')
        self.job = HarvestJobObj(source=source)
        self.job_id = self.job.id

    def _import(self, content):
        job = harvest_model.HarvestJob.get(self.job_id)
        harvest_object = HarvestObjectObj(
            guid=content['id'],
            job=job,
            content=json.dumps(content),
            extras={})
        obj_id = harvest_object.id
        result = self.harvester._create_or_update_package(
            json.loads(json.dumps(content)), harvest_object,
            package_dict_form='package_show')
        return result, harvest_model.HarvestObject.get(obj_id)

    def _show(self):
        return toolkit.get_action('package_show')(
            {'ignore_auth': True}, {'id': 'merge-dataset-id'})

    def _edit(self, **fields):
        toolkit.get_action('package_patch')(
            {'ignore_auth': True},
            dict(id='merge-dataset-id', **fields))

    def _conflicts(self, harvest_object):
        for extra in harvest_object.extras:
            if extra.key == MERGE_CONFLICTS_EXTRA:
                return json.loads(extra.value)
        return []

    def _merge_base(self, harvest_object):
        for extra in harvest_object.extras:
            if extra.key == MERGE_BASE_EXTRA:
                return json.loads(extra.value)
        return None

    def test_create_records_merge_base(self):
        result, harvest_object = self._import(_upstream_dict())

        assert result is True
        assert self._merge_base(harvest_object)['notes'] == \
            'Upstream description'

    def test_unmodified_local_fields_take_upstream(self):
        self._import(_upstream_dict())
        self._edit()

        updated = _upstream_dict(
            notes='New upstream description',
            license_id='cc-by-sa',
            metadata_modified='2020-02-01T00:00:00')
        result, _ = self._import(updated)

        assert result is True
        dataset = self._show()
        assert dataset['notes'] == 'New upstream description'
        assert dataset['license_id'] == 'cc-by-sa'

    def test_cataloger_edit_survives_upstream_metadata_change(self):
        self._import(_upstream_dict())
        self._edit(notes='Cataloger description')

        updated = _upstream_dict(
            license_id='cc-by-sa',
            title='Renamed upstream title',
            metadata_modified='2020-02-01T00:00:00')
        result, harvest_object = self._import(updated)

        assert result is True
        dataset = self._show()
        # The cataloger edit is preserved because upstream did not touch the
        # description...
        assert dataset['notes'] == 'Cataloger description'
        # ...while the other upstream changes are applied.
        assert dataset['license_id'] == 'cc-by-sa'
        assert dataset['title'] == 'Renamed upstream title'
        # Only the fields touched by both sides are recorded for review, and
        # here there are none.
        assert self._conflicts(harvest_object) == []

    def test_field_changed_by_both_sides_is_recorded(self):
        self._import(_upstream_dict())
        self._edit(notes='Cataloger description', license_id='ogl')

        updated = _upstream_dict(
            notes='Upstream description v2',
            license_id='cc-by-sa',
            metadata_modified='2020-02-01T00:00:00')
        result, harvest_object = self._import(updated)

        assert result is True
        dataset = self._show()
        # The description may be maintained by hand, so the local value wins.
        assert dataset['notes'] == 'Cataloger description'
        # The license is upstream metadata, so upstream wins.
        assert dataset['license_id'] == 'cc-by-sa'
        # Both conflicts are recorded for review.
        conflicts = {c['field']: c for c in self._conflicts(harvest_object)}
        assert set(conflicts) == {'notes', 'license_id'}
        assert conflicts['notes']['resolution'] == 'local'
        assert conflicts['notes']['local'] == 'Cataloger description'
        assert conflicts['notes']['upstream'] == 'Upstream description v2'
        assert conflicts['license_id']['resolution'] == 'upstream'
        assert conflicts['license_id']['base'] == 'cc-by'
        assert conflicts['license_id']['local'] == 'ogl'
        assert conflicts['license_id']['upstream'] == 'cc-by-sa'

    def test_upstream_change_wins_for_untouched_prioritized_field(self):
        self._import(_upstream_dict())
        self._edit(license_id='ogl')

        updated = _upstream_dict(
            notes='New upstream description',
            license_id='cc-by',
            metadata_modified='2020-02-01T00:00:00')
        result, harvest_object = self._import(updated)

        assert result is True
        assert self._show()['notes'] == 'New upstream description'
        assert self._show()['license_id'] == 'ogl'
        # No conflict: both sides did not change the notes.
        assert self._conflicts(harvest_object) == []

    def test_unchanged_upstream_version_keeps_merged_result(self):
        self._import(_upstream_dict())
        self._edit(notes='Cataloger description')

        # Upstream republishes exactly the same version.
        result, harvest_object = self._import(_upstream_dict())

        assert result == 'unchanged'
        assert self._show()['notes'] == 'Cataloger description'

    def test_reharvest_after_merge_keeps_result(self):
        self._import(_upstream_dict())
        self._edit(notes='Cataloger description')

        updated = _upstream_dict(license_id='cc-by-sa',
                                 metadata_modified='2020-02-01T00:00:00')
        self._import(updated)

        assert self._show()['notes'] == 'Cataloger description'

        # Same upstream version again: nothing else to merge, nothing lost.
        result, _ = self._import(updated)
        assert result == 'unchanged'
        assert self._show()['notes'] == 'Cataloger description'
        assert self._show()['license_id'] == 'cc-by-sa'

    def test_tags_are_merged_by_name(self):
        self._import(_upstream_dict())
        self._edit(tags=[{'name': 'alpha'}, {'name': 'cataloger-tag'}])

        updated = _upstream_dict(
            tags=[{'name': 'alpha'}, {'name': 'beta'}, {'name': 'gamma'}],
            metadata_modified='2020-02-01T00:00:00')
        result, harvest_object = self._import(updated)

        assert result is True
        names = sorted(tag['name'] for tag in self._show()['tags'])
        # The cataloger maintained the tags, so the local set wins.
        assert names == ['alpha', 'cataloger-tag']
        conflicts = {c['field']: c for c in self._conflicts(harvest_object)}
        assert conflicts['tags']['resolution'] == 'local'
        assert conflicts['tags']['upstream'] == ['alpha', 'beta', 'gamma']

    def test_extras_are_merged_key_by_key(self):
        self._import(_upstream_dict())
        # A cataloger adds an extra; upstream keeps its own extra.
        self._edit(extras=[{'key': 'owner', 'value': 'upstream'},
                           {'key': 'reviewed', 'value': 'yes'}])

        updated = _upstream_dict(
            extras=[{'key': 'owner', 'value': 'upstream'},
                    {'key': 'temporal', 'value': '2020'}],
            metadata_modified='2020-02-01T00:00:00')
        result, _ = self._import(updated)

        assert result is True
        extras = {extra['key']: extra['value']
                  for extra in self._show()['extras']
                  if not extra['key'].startswith('harvest_')}
        assert extras['owner'] == 'upstream'
        assert extras['temporal'] == '2020'
        assert extras['reviewed'] == 'yes'

    def test_resources_follow_upstream(self):
        self._import(_upstream_dict())
        self._edit(notes='Cataloger description')

        updated = _upstream_dict(
            notes='Upstream description',
            metadata_modified='2020-02-01T00:00:00')
        updated['resources'] = [{
            'id': 'f156019d-ea88-46a6-8fa3-3d12582e2161',
            'url': 'http://example.com/data-v2.csv',
            'format': 'CSV',
            'name': 'data v2',
        }]
        result, _ = self._import(updated)

        assert result is True
        dataset = self._show()
        assert dataset['resources'][0]['url'] == \
            'http://example.com/data-v2.csv'
        assert dataset['resources'][0]['name'] == 'data v2'
        assert dataset['notes'] == 'Cataloger description'

    @pytest.mark.ckan_config('ckan.harvest.merge_fields', 'notes title')
    def test_merge_fields_configurable_globally(self):
        assert self.harvester._get_merge_fields() == {'notes', 'title'}

    def test_merge_fields_default_to_description_and_tags(self):
        assert self.harvester._get_merge_fields() == {'notes', 'tags'}

    def test_merge_fields_configurable_per_source(self):
        self.harvester.config = {'merge_fields': ['notes', 'owner']}
        assert self.harvester._get_merge_fields() == {'notes', 'owner'}

    def test_no_base_falls_back_to_priority(self):
        # A dataset that existed before the merge base was recorded: no
        # ``merge_base`` extra and no previous harvest object content.
        self._import(_upstream_dict())
        for obj in model.Session.query(harvest_model.HarvestObject).all():
            for extra in list(obj.extras):
                if extra.key in (MERGE_BASE_EXTRA, MERGE_CONFLICTS_EXTRA):
                    obj.extras.remove(extra)
            obj.content = None
        model.Session.commit()

        self._edit(notes='Cataloger description', license_id='ogl')
        updated = _upstream_dict(
            notes='Upstream description', license_id='cc-by-sa',
            metadata_modified='2020-02-01T00:00:00')
        result, _ = self._import(updated)

        assert result is True
        dataset = self._show()
        assert dataset['notes'] == 'Cataloger description'
        assert dataset['license_id'] == 'cc-by-sa'


class TestMergeAlgorithm(object):
    '''Unit tests for the pure three-way merge, without a database.'''

    def _merge(self, base, local, upstream, merge_fields=None,
               harvester=None):
        harvester = harvester or HarvesterBase()
        harvester.config = ({'merge_fields': merge_fields}
                            if merge_fields is not None else {})
        harvest_object = type('Obj', (), {'guid': 'guid', 'extras': []})()
        merged, conflicts = harvester._merge_package_fields(
            upstream, local, harvest_object,
            merge_base=base)
        return merged, conflicts

    def test_local_untouched_takes_upstream(self):
        base = {'notes': 'a', 'license_id': 'x'}
        local = {'notes': 'a', 'license_id': 'x'}
        upstream = {'notes': 'b', 'license_id': 'x'}
        merged, conflicts = self._merge(base, local, upstream)
        assert merged['notes'] == 'b'
        assert conflicts == []

    def test_upstream_untouched_keeps_local(self):
        base = {'notes': 'a', 'license_id': 'x'}
        local = {'notes': 'local', 'license_id': 'x'}
        upstream = {'notes': 'a', 'license_id': 'x'}
        merged, conflicts = self._merge(base, local, upstream)
        assert merged['notes'] == 'local'
        assert conflicts == []

    def test_both_changed_prioritized_field_keeps_local(self):
        base = {'notes': 'a'}
        local = {'notes': 'local'}
        upstream = {'notes': 'upstream'}
        merged, conflicts = self._merge(base, local, upstream)
        assert merged['notes'] == 'local'
        assert [c['field'] for c in conflicts] == ['notes']
        assert conflicts[0]['resolution'] == 'local'

    def test_both_changed_non_prioritized_field_takes_upstream(self):
        base = {'license_id': 'a'}
        local = {'license_id': 'local'}
        upstream = {'license_id': 'upstream'}
        merged, conflicts = self._merge(base, local, upstream)
        assert merged['license_id'] == 'upstream'
        assert conflicts[0]['resolution'] == 'upstream'

    def test_missing_field_is_a_deletion(self):
        base = {'notes': 'a', 'license_id': 'x'}
        local = {'notes': 'a', 'license_id': 'x'}
        upstream = {'notes': 'a'}
        merged, conflicts = self._merge(base, local, upstream)
        assert 'license_id' not in merged
        assert conflicts == []

    def test_local_addition_is_kept_for_prioritized_field(self):
        base = {'notes': 'a'}
        local = {'notes': 'a', 'author': 'cataloger'}
        upstream = {'notes': 'a'}
        merged, conflicts = self._merge(
            base, local, upstream, merge_fields=['notes', 'author'])
        assert merged['author'] == 'cataloger'
        assert conflicts == []

    def test_rest_form_tags_are_strings(self):
        base = {'tags': ['a', 'b']}
        local = {'tags': ['a', 'b']}
        upstream = {'tags': ['a', 'b', 'c']}
        merged, conflicts = self._merge(base, local, upstream)
        assert merged['tags'] == ['a', 'b', 'c']
        assert conflicts == []

    def test_rest_form_extras_are_a_dict(self):
        base = {'extras': {'owner': 'upstream'}}
        local = {'extras': {'owner': 'upstream'}}
        upstream = {'extras': {'owner': 'upstream', 'temporal': '2020'}}
        merged, conflicts = self._merge(base, local, upstream)
        assert merged['extras'] == {'owner': 'upstream', 'temporal': '2020'}

    def test_no_base_prefers_local_for_prioritized_field(self):
        harvester = HarvesterBase()
        harvester.config = {}
        harvest_object = type('Obj', (), {
            'guid': 'guid', 'id': 'obj-id', 'extras': []})()
        merged, conflicts = harvester._merge_package_fields(
            {'notes': 'upstream', 'license_id': 'upstream'},
            {'notes': 'local', 'license_id': 'local'},
            harvest_object, merge_base=None)
        assert merged['notes'] == 'local'
        assert merged['license_id'] == 'upstream'
        assert conflicts == []
