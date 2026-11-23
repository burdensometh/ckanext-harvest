'''
Tests for harvesting several CKAN sources that expose datasets sharing the
same remote dataset id.

The aggregating catalog must publish and update both datasets, keep a stable
local mapping per source and never let one source update the package owned by
another source.
'''
from __future__ import absolute_import

import copy
import json
import re
from http.server import BaseHTTPRequestHandler
from socketserver import TCPServer
from threading import Thread
from urllib.parse import unquote_plus

import pytest

from ckan import model
from ckan.plugins import toolkit
from ckantoolkit.tests.factories import Organization

from ckanext.harvest.tests.factories import HarvestSourceObj, HarvestJobObj
from ckanext.harvest.tests.lib import run_harvest_job
import ckanext.harvest.model as harvest_model
from ckanext.harvest.harvesters.ckanharvester import CKANHarvester


PORT = 8999

# The datasets "served" by the second mock CKAN. Tests can replace the
# contents of this list while a server is running.
DATASETS = []


class CollisionMockHandler(BaseHTTPRequestHandler):
    '''Minimal mock of the CKAN package_search API used by the harvester.'''

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if '/api/3/action/package_search' not in self.path:
            self.respond('Not implemented', status=400)
            return
        params = self.get_url_params()
        # paging: only return results on the first page, empty pages after
        start = int(params.get('start', 0))
        results = DATASETS if start == 0 else []
        # Incremental harvests ask for datasets modified since a given date:
        # honor it by comparing the metadata_modified values.
        fq = params.get('fq', '')
        match = re.search(r'metadata_modified:\[([^Z]+)Z TO \*\]', fq)
        if match:
            since = match.group(1)
            results = [d for d in results
                       if d.get('metadata_modified', '') >= since]
        self.respond_action({'count': len(results), 'results': results})

    def get_url_params(self):
        if '?' not in self.path:
            return {}
        params_str = self.path.split('?', 1)[1]
        params = unquote_plus(params_str).split('&')
        return dict(param.split('=') for param in params)

    def respond_action(self, result_dict, status=200):
        self.respond(json.dumps({'result': result_dict, 'success': True}),
                     status=status)

    def respond(self, content, status=200):
        payload = content.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def serve():
    class TestServer(TCPServer):
        allow_reuse_address = True

    httpd = TestServer(('', PORT), CollisionMockHandler)
    thread = Thread(target=httpd.serve_forever)
    thread.daemon = True
    thread.start()


serve()


def make_dataset(dataset_id, name, title, metadata_modified,
                 resources=None):
    return {
        'id': dataset_id,
        'name': name,
        'title': title,
        'state': 'active',
        'type': 'dataset',
        'metadata_modified': metadata_modified,
        'metadata_created': '2020-01-01T00:00:00.000000',
        'owner_org': None,
        'tags': [],
        'groups': [],
        'extras': [],
        'resources': resources or [],
    }


def harvest_source(url, org):
    return HarvestSourceObj(url=url, source_type='ckan',
                            owner_org=org['id'])


@pytest.mark.usefixtures('with_plugins', 'clean_db', 'clean_index')
class TestCkanHarvesterIdCollisions(object):

    URL = 'http://localhost:%s/' % PORT

    def _harvest(self, source):
        job = HarvestJobObj(source=source, run=False)
        return run_harvest_job(job, CKANHarvester())

    def _make_legacy_object(self, source, remote_id, package_id, dataset):
        '''
        Builds a harvest object as produced by harvester versions before
        guids were scoped per source (guid == bare remote id), attached to a
        finished historical job.
        '''
        legacy_job = HarvestJobObj(source=source, run=False)
        legacy_job.status = 'Finished'
        legacy_job.gather_started = legacy_job.created
        legacy_job.gather_finished = legacy_job.created
        legacy_job.save()
        legacy_object = harvest_model.HarvestObject(
            guid=remote_id,
            job=legacy_job,
            harvest_source_id=source.id,
            content=json.dumps(dataset),
            package_id=package_id,
            state='COMPLETE',
            current=True,
            import_finished=legacy_job.created)
        legacy_object.save()
        model.Session.commit()
        return legacy_object

    def test_colliding_ids_create_two_distinct_packages(self):
        # Both remote instances number a dataset with the same id, but they
        # are different datasets.
        shared_remote_id = 'shared-dataset-id'
        source_a = harvest_source(self.URL + 'a/', Organization())
        source_b = harvest_source(self.URL + 'b/', Organization())

        dataset_a = make_dataset(shared_remote_id, 'alpha-dataset',
                                 'Alpha Dataset',
                                 '2099-01-01T00:00:00.000000')
        dataset_b = make_dataset(shared_remote_id, 'beta-dataset',
                                 'Beta Dataset',
                                 '2099-01-02T00:00:00.000000')

        global DATASETS
        DATASETS = [dataset_a]
        results_a = self._harvest(source_a)
        DATASETS = [dataset_b]
        results_b = self._harvest(source_b)

        package_a_id = results_a[shared_remote_id]['dataset']['id']
        package_b_id = results_b[shared_remote_id]['dataset']['id']

        # Two distinct local packages were published.
        assert package_a_id != package_b_id
        assert package_a_id != shared_remote_id
        assert package_b_id != shared_remote_id

        package_a = toolkit.get_action('package_show')(
            {'ignore_auth': True}, {'id': package_a_id})
        package_b = toolkit.get_action('package_show')(
            {'ignore_auth': True}, {'id': package_b_id})
        assert package_a['title'] == 'Alpha Dataset'
        assert package_b['title'] == 'Beta Dataset'

        # The guids are unique across sources and scoped to each source.
        guid_a = results_a[shared_remote_id]['guid']
        guid_b = results_b[shared_remote_id]['guid']
        assert guid_a == CKANHarvester._guid_for_remote_id(
            source_a.id, shared_remote_id)
        assert guid_b == CKANHarvester._guid_for_remote_id(
            source_b.id, shared_remote_id)
        assert guid_a != guid_b

    def test_updates_keep_stable_local_identity_per_source(self):
        shared_remote_id = 'shared-dataset-id'
        source_a = harvest_source(self.URL + 'a/', Organization())
        source_b = harvest_source(self.URL + 'b/', Organization())

        dataset_a = make_dataset(shared_remote_id, 'alpha-dataset',
                                 'Alpha Dataset',
                                 '2099-01-01T00:00:00.000000')
        dataset_b = make_dataset(shared_remote_id, 'beta-dataset',
                                 'Beta Dataset',
                                 '2099-01-02T00:00:00.000000')

        global DATASETS
        DATASETS = [dataset_a]
        results_a = self._harvest(source_a)
        DATASETS = [dataset_b]
        results_b = self._harvest(source_b)
        package_a_id = results_a[shared_remote_id]['dataset']['id']
        package_b_id = results_b[shared_remote_id]['dataset']['id']

        # Source A re-harvests an updated version of its dataset.
        dataset_a_updated = copy.deepcopy(dataset_a)
        dataset_a_updated['title'] = 'Alpha Dataset Updated'
        dataset_a_updated['metadata_modified'] = '2099-03-01T00:00:00.000000'
        DATASETS = [dataset_a_updated]
        results_a_again = self._harvest(source_a)

        assert results_a_again[shared_remote_id]['report_status'] == 'updated'
        # The same local package is reused (stable mapping).
        assert (results_a_again[shared_remote_id]['dataset']['id']
                == package_a_id)

        # Source B's package is untouched: same id and unchanged content.
        package_b = toolkit.get_action('package_show')(
            {'ignore_auth': True}, {'id': package_b_id})
        assert package_b['title'] == 'Beta Dataset'

        # And vice versa: an update from source B cannot modify source A's
        # package.
        dataset_b_updated = copy.deepcopy(dataset_b)
        dataset_b_updated['title'] = 'Beta Dataset Updated'
        dataset_b_updated['metadata_modified'] = '2099-03-02T00:00:00.000000'
        DATASETS = [dataset_b_updated]
        results_b_again = self._harvest(source_b)
        assert (results_b_again[shared_remote_id]['dataset']['id']
                == package_b_id)

        package_a = toolkit.get_action('package_show')(
            {'ignore_auth': True}, {'id': package_a_id})
        assert package_a['title'] == 'Alpha Dataset Updated'

    def test_colliding_resource_ids_do_not_move_between_packages(self):
        shared_remote_id = 'shared-dataset-id'
        shared_resource_id = 'shared-resource-id'
        source_a = harvest_source(self.URL + 'a/', Organization())
        source_b = harvest_source(self.URL + 'b/', Organization())

        dataset_a = make_dataset(
            shared_remote_id, 'alpha-dataset', 'Alpha Dataset',
            '2099-01-01T00:00:00.000000',
            resources=[{'id': shared_resource_id,
                        'url': 'http://example.com/a.csv',
                        'name': 'alpha.csv'}])
        dataset_b = make_dataset(
            shared_remote_id, 'beta-dataset', 'Beta Dataset',
            '2099-01-02T00:00:00.000000',
            resources=[{'id': shared_resource_id,
                        'url': 'http://example.com/b.csv',
                        'name': 'beta.csv'}])

        global DATASETS
        DATASETS = [dataset_a]
        results_a = self._harvest(source_a)
        DATASETS = [dataset_b]
        results_b = self._harvest(source_b)

        package_a = results_a[shared_remote_id]['dataset']
        package_b = results_b[shared_remote_id]['dataset']

        # Each package keeps its own resource; importing source B must not
        # steal source A's resource.
        assert len(package_a['resources']) == 1
        assert len(package_b['resources']) == 1
        local_resource_a = package_a['resources'][0]
        local_resource_b = package_b['resources'][0]
        assert local_resource_a['id'] != shared_resource_id
        assert local_resource_b['id'] != shared_resource_id
        assert local_resource_a['id'] != local_resource_b['id']
        assert local_resource_a['url'] == 'http://example.com/a.csv'
        assert local_resource_b['url'] == 'http://example.com/b.csv'

        # Re-check straight from the DB to make sure both resources still
        # belong to their original packages.
        refreshed_a = toolkit.get_action('package_show')(
            {'ignore_auth': True}, {'id': package_a['id']})
        refreshed_b = toolkit.get_action('package_show')(
            {'ignore_auth': True}, {'id': package_b['id']})
        assert refreshed_a['resources'][0]['url'] == \
            'http://example.com/a.csv'
        assert refreshed_b['resources'][0]['url'] == \
            'http://example.com/b.csv'

    def test_legacy_guid_objects_keep_their_local_identity(self):
        # Simulate a dataset imported by an older version of the harvester,
        # where the guid was the bare remote id and the local package reused
        # that id.
        org = Organization()
        source = harvest_source(self.URL, org)
        # Remote CKAN ids are UUIDs, so a realistic legacy local package id
        # is a valid UUID too.
        remote_id = '0d54b87a-2c4f-4e08-9b18-9a0e4f6c1111'
        legacy_dataset = make_dataset(remote_id, 'legacy-dataset',
                                      'Legacy Dataset',
                                      '2099-01-01T00:00:00.000000')

        # Create the local package first (the legacy harvest object has a FK
        # to it), as the old harvester did using the remote id.
        toolkit.get_action('package_create')(
            {'ignore_auth': True},
            {'id': remote_id, 'name': 'legacy-dataset',
             'title': 'Legacy Dataset', 'owner_org': org['id']})
        self._make_legacy_object(source, remote_id, remote_id, legacy_dataset)

        # The new harvester re-imports the same dataset from the same source.
        global DATASETS
        DATASETS = [legacy_dataset]
        results = self._harvest(source)

        # The pre-existing local identity is reused rather than creating a new
        # package.
        assert results[remote_id]['dataset']['id'] == remote_id
        assert results[remote_id]['report_status'] in (
            'updated', 'not modified')

        # And the freshly gathered object now carries the source-scoped guid.
        new_object = model.Session.query(harvest_model.HarvestObject).filter(
            harvest_model.HarvestObject.harvest_source_id == source.id,
            harvest_model.HarvestObject.guid ==
            CKANHarvester._guid_for_remote_id(source.id, remote_id)).one()
        assert new_object.package_id == remote_id

    def test_legacy_guid_from_other_source_cannot_be_taken_over(self):
        # Source A holds a legacy-style package (local id == remote id).
        # Source B then harvests a *different* dataset that happens to use the
        # same remote id. Source B must not update source A's package.
        shared_remote_id = '0d54b87a-2c4f-4e08-9b18-9a0e4f6c2222'
        org_a = Organization()
        org_b = Organization()
        source_a = harvest_source(self.URL + 'a/', org_a)
        source_b = harvest_source(self.URL + 'b/', org_b)

        package_a = toolkit.get_action('package_create')(
            {'ignore_auth': True},
            {'id': shared_remote_id, 'name': 'source-a-dataset',
             'title': 'Source A Dataset', 'owner_org': org_a['id']})
        self._make_legacy_object(
            source_a, shared_remote_id, package_a['id'],
            make_dataset(shared_remote_id, 'source-a-dataset',
                         'Source A Dataset',
                         '2099-01-01T00:00:00.000000'))

        dataset_b = make_dataset(shared_remote_id, 'source-b-dataset',
                                 'Source B Dataset',
                                 '2099-01-02T00:00:00.000000')
        global DATASETS
        DATASETS = [dataset_b]
        results_b = self._harvest(source_b)

        package_b_id = results_b[shared_remote_id]['dataset']['id']
        assert package_b_id != shared_remote_id

        # Both packages coexist, and source A's package was not modified.
        refreshed_a = toolkit.get_action('package_show')(
            {'ignore_auth': True}, {'id': package_a['id']})
        assert refreshed_a['title'] == 'Source A Dataset'
        refreshed_b = toolkit.get_action('package_show')(
            {'ignore_auth': True}, {'id': package_b_id})
        assert refreshed_b['title'] == 'Source B Dataset'

        # Re-harvesting source B keeps mapping to its own package.
        dataset_b_updated = copy.deepcopy(dataset_b)
        dataset_b_updated['title'] = 'Source B Dataset Updated'
        dataset_b_updated['metadata_modified'] = '2099-03-01T00:00:00.000000'
        DATASETS = [dataset_b_updated]
        results_b_again = self._harvest(source_b)
        assert (results_b_again[shared_remote_id]['dataset']['id']
                == package_b_id)
        refreshed_a = toolkit.get_action('package_show')(
            {'ignore_auth': True}, {'id': package_a['id']})
        assert refreshed_a['title'] == 'Source A Dataset'
