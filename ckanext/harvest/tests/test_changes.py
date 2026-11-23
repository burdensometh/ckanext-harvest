# -*- coding: utf-8 -*-
"""Tests for the harvest change feed.

Covers:

* change records written from successfully published harvest objects,
  with field-level title / license / resource diffs;
* idempotent recording, so re-processing an object never duplicates a
  row;
* stable keyset pagination and filtering of the change feed;
* failures (object and gather errors) exposed through a separate feed;
* change records removed together with their harvest objects.
"""
import copy
import json
import uuid

import pytest
from unittest.mock import patch

from ckan import model
from ckan.plugins import toolkit

from ckanext.harvest import queue
from ckanext.harvest.changes import record_harvest_object_change
from ckanext.harvest.model import (
    HarvestObject,
    HarvestObjectChange,
    HarvestObjectError,
    HarvestGatherError,
)
from ckanext.harvest.tests.factories import (
    HarvestSourceObj,
    HarvestJobObj,
    HarvestObjectObj,
)
from ckanext.harvest.tests.lib import run_harvest
from ckanext.harvest.harvesters.ckanharvester import CKANHarvester

from .harvesters import mock_ckan

mock_ckan.serve()

# CKAN validates resource ids as UUIDs
RES_1 = "11111111-1111-1111-1111-111111111111"
RES_2 = "22222222-2222-2222-2222-222222222222"


def _resource(rid, name=None, url=None, fmt="CSV"):
    return {
        "id": rid,
        "name": name or ("Resource %s" % rid[:8]),
        "url": url or "http://example.com/%s" % rid,
        "format": fmt,
    }


def _package_dict(guid="guid-1", title="Original title",
                  license_id="cc-by", resources=None):
    if resources is None:
        # Resource ids are globally unique in CKAN, derive one from the
        # dataset guid so different datasets never collide.
        resource_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, guid))
        resources = [
            _resource(resource_id, name="First resource",
                      url="http://example.com/%s/res-1.csv" % guid)
        ]
    return {
        "id": guid,
        "name": "dataset-%s" % guid,
        "title": title,
        "license_id": license_id,
        "license_title": "Creative Commons Attribution",
        "metadata_modified": "2020-01-01T00:00:00",
        "resources": resources if resources is not None else [
            _resource(RES_1, name="First resource",
                      url="http://example.com/res-1.csv"),
        ],
        "extras": [],
        "tags": [],
        "groups": [],
    }


@pytest.mark.usefixtures("with_plugins", "clean_db", "clean_queues",
                         "clean_index", "writable_storage_path")
@pytest.mark.ckan_config(
    "ckan.plugins", "harvest ckan_harvester test_harvester"
)
class TestHarvestChanges:

    def _run_ckan_harvest(self, datasets):
        with patch(
            "ckanext.harvest.tests.harvesters.mock_ckan.DATASETS",
            datasets,
        ):
            return run_harvest(
                url="http://localhost:%s/" % mock_ckan.PORT,
                harvester=CKANHarvester(),
            )

    def test_added_change_created_on_successful_import(self):
        datasets = [_package_dict()]
        self._run_ckan_harvest(datasets)

        changes = model.Session.query(HarvestObjectChange).all()
        assert len(changes) == 1
        change = changes[0]
        assert change.change_type == "added"
        assert change.guid == "guid-1"
        assert change.package_id
        assert change.harvest_source_id
        assert change.harvest_job_id
        assert change.created is not None

        details = change.details
        assert details["title"] == "Original title"
        assert details["license"] == {
            "id": "cc-by",
            "title": "Creative Commons Attribution",
        }
        expected_resource = str(
            uuid.uuid5(uuid.NAMESPACE_DNS, "guid-1")
        )
        assert [r["id"] for r in details["resources"]] == [
            expected_resource
        ]

    def test_updated_change_contains_field_level_diff(self):
        self._run_ckan_harvest([_package_dict(resources=[
            _resource(RES_1, name="First resource",
                      url="http://example.com/res-1.csv"),
        ])])

        updated = _package_dict(
            title="New title",
            license_id="odc-by",
            resources=[
                # RES_1 unchanged, RES_2 added
                _resource(RES_1, name="First resource",
                          url="http://example.com/res-1.csv"),
                _resource(RES_2, name="Second resource",
                          url="http://example.com/res-2.json", fmt="JSON"),
            ],
        )
        updated["metadata_modified"] = "2050-01-01T00:00:00"
        self._run_ckan_harvest([updated])

        changes = (
            model.Session.query(HarvestObjectChange)
            .order_by(HarvestObjectChange.created)
            .all()
        )
        assert [c.change_type for c in changes] == ["added", "updated"]

        details = changes[-1].details
        assert set(details["changed_fields"]) == {
            "title", "license", "resources"
        }
        assert details["title"] == {
            "old": "Original title", "new": "New title"
        }
        assert details["license"]["old"]["id"] == "cc-by"
        assert details["license"]["new"]["id"] == "odc-by"
        assert [r["id"] for r in details["resources"]["added"]] == [
            RES_2
        ]
        assert details["resources"]["removed"] == []
        assert [
            r["id"] for r in details["resources"]["unchanged"]
        ] == [RES_1]

    def test_updated_change_tracks_removed_resources(self):
        self._run_ckan_harvest([_package_dict(resources=[
            _resource(RES_1, url="http://example.com/1"),
            _resource(RES_2, url="http://example.com/2"),
        ])])

        updated = _package_dict(resources=[
            _resource(RES_2, url="http://example.com/2"),
        ])
        updated["metadata_modified"] = "2050-01-01T00:00:00"
        self._run_ckan_harvest([updated])

        update_change = (
            model.Session.query(HarvestObjectChange)
            .filter_by(change_type="updated")
            .one()
        )
        resources = update_change.details["resources"]
        assert [r["id"] for r in resources["removed"]] == [RES_1]
        assert resources["added"] == []

    def test_unchanged_and_errored_objects_have_no_change_record(self):
        # First harvest publishes the dataset.
        datasets = [_package_dict()]
        self._run_ckan_harvest(datasets)

        # Second harvest with identical metadata: report_status will be
        # 'not modified', no new change record must appear.
        self._run_ckan_harvest(copy.deepcopy(datasets))

        assert model.Session.query(HarvestObjectChange).count() == 1

        # An errored object must not create a change record either.
        source = HarvestSourceObj(source_type="test")
        job = HarvestJobObj(source=source, run=False)
        obj = HarvestObjectObj(
            guid="broken-guid",
            job=job,
            source_id=source.id,
            content="not-json",
        )
        obj.state = "ERROR"
        obj.report_status = "errored"
        obj.save()
        HarvestObjectError.create(
            "something went wrong", obj, "Import"
        )
        assert record_harvest_object_change(obj) is None
        assert model.Session.query(HarvestObjectChange).count() == 1

    def test_recording_is_idempotent_for_same_object(self):
        datasets = [_package_dict()]
        self._run_ckan_harvest(datasets)

        obj = (
            model.Session.query(HarvestObject)
            .filter_by(report_status="added")
            .one()
        )
        first = record_harvest_object_change(obj)
        second = record_harvest_object_change(obj)

        assert first.id == second.id
        assert model.Session.query(HarvestObjectChange).count() == 1

    def test_reprocessing_object_through_stages_does_not_duplicate(self):
        datasets = [_package_dict()]
        self._run_ckan_harvest(datasets)

        # Run the fetch/import stages again on the same object, as a
        # retry or a manual reimport would.
        obj = (
            model.Session.query(HarvestObject)
            .filter_by(current=True)
            .one()
        )
        harvester = CKANHarvester()
        queue.fetch_and_import_stages(harvester, obj)

        assert model.Session.query(HarvestObjectChange).count() == 1

    def test_change_feed_action_pagination_and_filters(self):
        datasets = [
            _package_dict(guid="guid-a", title="Dataset A"),
            _package_dict(guid="guid-b", title="Dataset B"),
        ]
        self._run_ckan_harvest(datasets)
        # One update on guid-a so both change types are represented.
        updated = _package_dict(
            guid="guid-a",
            title="Dataset A renamed",
        )
        updated["metadata_modified"] = "2050-01-01T00:00:00"
        self._run_ckan_harvest([updated])

        action = toolkit.get_action("harvest_changes_list")
        ctx = {"ignore_auth": True}

        page1 = action(ctx, {"limit": 2})
        assert page1["count"] == 3
        assert len(page1["results"]) == 2
        assert page1["next_cursor"]

        page2 = action(
            ctx,
            {"limit": 2, "cursor": page1["next_cursor"]},
        )
        assert len(page2["results"]) == 1
        assert page2["next_cursor"] is None

        # The two pages cover every record exactly once
        seen = [r["id"] for r in page1["results"]] + [
            r["id"] for r in page2["results"]
        ]
        assert len(seen) == len(set(seen)) == 3

        # Ordering is newest first and stable
        ordered = action(ctx, {"limit": 10})["results"]
        assert [r["change_type"] for r in ordered][0] == "updated"
        for earlier, later in zip(ordered, ordered[1:]):
            assert (earlier["created"], earlier["id"]) > (
                later["created"], later["id"]
            )

        # Type filter
        only_added = action(ctx, {"change_type": "added"})
        assert only_added["count"] == 2
        assert all(
            r["change_type"] == "added" for r in only_added["results"]
        )

        # Source identity and source metadata are present
        record = ordered[0]
        assert record["guid"]
        assert record["harvest_source"]["id"] == record[
            "harvest_source_id"
        ]
        assert record["harvest_source"]["url"]
        assert record["package_id"]

        # Source filter
        source_id = record["harvest_source_id"]
        filtered = action(ctx, {"source_id": source_id})
        assert filtered["count"] == 3
        assert action(
            ctx, {"source_id": "does-not-exist"}
        )["count"] == 0

        # Time window filter
        since = action(ctx, {"since": "2099-01-01T00:00:00"})
        assert since["count"] == 0
        until = action(ctx, {"until": "2000-01-01T00:00:00"})
        assert until["count"] == 0

    def test_invalid_feed_parameters_raise_validation_error(self):
        action = toolkit.get_action("harvest_changes_list")
        ctx = {"ignore_auth": True}

        with pytest.raises(toolkit.ValidationError):
            action(ctx, {"change_type": "nope"})
        with pytest.raises(toolkit.ValidationError):
            action(ctx, {"limit": 0})
        with pytest.raises(toolkit.ValidationError):
            action(ctx, {"cursor": "garbage"})

    def test_failed_feed_lists_object_and_gather_errors(self):
        source = HarvestSourceObj(source_type="test")
        job = HarvestJobObj(source=source, run=False)

        # Object-level failure: two messages on a single object must
        # collapse into one feed entry.
        broken = HarvestObjectObj(
            guid="broken-guid",
            job=job,
            source_id=source.id,
            state="ERROR",
            report_status="errored",
        )
        HarvestObjectError.create("first failure", broken, "Fetch")
        HarvestObjectError.create("second failure", broken, "Import")

        # Gather-level failure (no object could be gathered at all)
        HarvestGatherError.create("gather exploded", job)
        gather_error = (
            model.Session.query(HarvestGatherError)
            .filter_by(message="gather exploded")
            .one()
        )

        action = toolkit.get_action("harvest_changes_failed_list")
        feed = action({"ignore_auth": True}, {})

        assert feed["count"] == 2
        types = {row["failure_type"] for row in feed["results"]}
        assert types == {"object_error", "gather_error"}

        object_row = next(
            row for row in feed["results"]
            if row["failure_type"] == "object_error"
        )
        assert object_row["guid"] == "broken-guid"
        assert object_row["harvest_source_id"] == source.id
        assert len(object_row["errors"]) == 2
        messages = {e["message"] for e in object_row["errors"]}
        assert messages == {"first failure", "second failure"}

        gather_row = next(
            row for row in feed["results"]
            if row["failure_type"] == "gather_error"
        )
        assert gather_row["id"] == gather_error.id
        assert gather_row["errors"][0]["stage"] == "Gather"

        # The two feeds stay independent: successful changes never leak
        # into the failure feed and vice versa.
        assert model.Session.query(HarvestObjectChange).count() == 0

    def test_failed_feed_pagination_is_stable(self):
        source = HarvestSourceObj(source_type="test")
        job = HarvestJobObj(source=source, run=False)
        for i in range(3):
            obj = HarvestObjectObj(
                guid="broken-%s" % i,
                job=job,
                source_id=source.id,
                state="ERROR",
                report_status="errored",
            )
            HarvestObjectError.create("failure %s" % i, obj, "Import")

        action = toolkit.get_action("harvest_changes_failed_list")
        ctx = {"ignore_auth": True}
        page1 = action(ctx, {"limit": 2})
        assert len(page1["results"]) == 2
        page2 = action(
            ctx, {"limit": 2, "cursor": page1["next_cursor"]}
        )
        assert len(page2["results"]) == 1
        ids = [r["id"] for r in page1["results"]] + [
            r["id"] for r in page2["results"]
        ]
        assert len(set(ids)) == 3

    def test_failed_feed_source_filter(self):
        source1 = HarvestSourceObj(source_type="test")
        source2 = HarvestSourceObj(source_type="test")
        job1 = HarvestJobObj(source=source1, run=False)
        job2 = HarvestJobObj(source=source2, run=False)

        obj1 = HarvestObjectObj(
            guid="broken-1", job=job1, source_id=source1.id,
            state="ERROR", report_status="errored",
        )
        HarvestObjectError.create("failure 1", obj1, "Import")
        obj2 = HarvestObjectObj(
            guid="broken-2", job=job2, source_id=source2.id,
            state="ERROR", report_status="errored",
        )
        HarvestObjectError.create("failure 2", obj2, "Import")

        action = toolkit.get_action("harvest_changes_failed_list")
        feed = action(
            {"ignore_auth": True}, {"source_id": source1.id}
        )
        assert feed["count"] == 1
        assert feed["results"][0]["guid"] == "broken-1"

    def test_change_records_cascade_with_harvest_object(self):
        datasets = [_package_dict()]
        self._run_ckan_harvest(datasets)
        assert model.Session.query(HarvestObjectChange).count() == 1

        obj = model.Session.query(HarvestObject).one()
        model.Session.delete(obj)
        model.Session.commit()

        assert model.Session.query(HarvestObjectChange).count() == 0

    def test_object_without_usable_content_falls_back_to_package(self):
        source = HarvestSourceObj(source_type="test")
        job = HarvestJobObj(source=source, run=False)
        package = toolkit.get_action("package_create")(
            {"ignore_auth": True},
            {"name": "fallback-dataset", "title": "Fallback title",
             "license_id": "cc-zero"},
        )
        obj = HarvestObjectObj(
            guid="fallback-guid",
            job=job,
            source_id=source.id,
            package_id=package["id"],
            content=None,
        )
        # The harvest_object_create action does not accept status
        # fields, so set them directly on the ORM object, as the queue
        # does after a successful import.
        obj.current = True
        obj.state = "COMPLETE"
        obj.report_status = "added"
        obj.save()

        change = record_harvest_object_change(obj)
        details = change.details
        assert details["title"] == "Fallback title"
        assert details["license"]["id"] == "cc-zero"
