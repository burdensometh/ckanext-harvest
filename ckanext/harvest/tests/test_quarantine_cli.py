# -*- coding: utf-8 -*-
"""Tests for the quarantine CLI commands."""

import json
import uuid

import pytest

from ckan.cli.cli import ckan

from ckanext.harvest.logic.quarantine import quarantine_object, list_entries
from ckanext.harvest.model import HarvestObject
from ckanext.harvest.tests import factories as harvest_factories

#: The record id must look like a CKAN id so the import stage can run.
GUID = str(uuid.uuid4())


def _create_entry(source_obj, guid=GUID):
    job = harvest_factories.HarvestJobObj(source=source_obj)
    harvest_object = HarvestObject(
        guid=guid,
        job=job,
        content=json.dumps({
            'id': guid,
            'name': 'quarantined-dataset',
            'title': 'Quarantined dataset',
            'license_id': 'not-agreed',
        }),
    )
    harvest_object.save()
    quarantine_object(harvest_object, [
        {'field': 'license_id', 'reason': 'not_allowed',
         'value': 'not-agreed', 'allowed': ['cc-by']},
    ])
    return harvest_object


@pytest.mark.usefixtures('with_plugins', 'clean_db', 'clean_index')
class TestQuarantineCli(object):

    def test_quarantine_list_shows_entries(self, cli):
        source_obj = harvest_factories.HarvestSourceObj()
        _create_entry(source_obj)

        result = cli.invoke(ckan, ['harvester', 'quarantine-list'])

        assert result.exit_code == 0
        assert GUID in result.output
        assert 'license_id' in result.output

    def test_quarantine_list_filters_by_source(self, cli):
        source_obj = harvest_factories.HarvestSourceObj()
        _create_entry(source_obj)

        result = cli.invoke(
            ckan, ['harvester', 'quarantine-list', '--source', source_obj.id])

        assert result.exit_code == 0
        assert GUID in result.output

    def test_quarantine_replay_requires_a_target(self, cli):
        result = cli.invoke(ckan, ['harvester', 'quarantine-replay'])
        assert result.exit_code != 0

    def test_quarantine_replay_single_entry(self, cli):
        source_obj = harvest_factories.HarvestSourceObj()
        _create_entry(source_obj)
        entry = list_entries()[0]

        # Correct the rules so the record complies.
        source_obj.config = json.dumps({
            'field_rules': {
                'required': ['title'],
                'allowed': {'license_id': ['not-agreed']},
            }
        })
        source_obj.save()

        result = cli.invoke(
            ckan, ['harvester', 'quarantine-replay', '--id', entry.id])

        assert result.exit_code == 0
        assert 'replayed' in result.output
        assert '1 record(s) processed' in result.output
