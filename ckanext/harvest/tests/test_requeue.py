# -*- coding: utf-8 -*-
"""
Tests for the retry/requeue machinery:

* gather/object errors are stored with a retryable flag distinguishing
  temporary upstream failures from permanent ones;
* ``harvest_objects_requeue`` only requeues recoverable objects of the given
  job, preserving successful objects and the full error history;
* the per-object retry budget (``ckan.harvest.max_retries``) caps how much
  queue capacity a single object can consume.
"""
import json

import pytest
try:
    from unittest.mock import patch
except ImportError:
    from mock import patch

from ckan import model
from ckan.lib.base import config
from ckan.plugins import toolkit

from ckanext.harvest.model import (
    HarvestObject,
    HarvestJob,
    HarvestGatherError,
    HarvestObjectError,
    TransientHarvestError,
)
from ckanext.harvest.tests.factories import (
    HarvestSourceObj,
    HarvestJobObj,
)
import ckanext.harvest.queue as queue


def _admin_context():
    user = toolkit.get_action(
        "get_site_user",
    )({"model": model, "ignore_auth": True}, {})["name"]
    return {
        "model": model,
        "session": model.Session,
        "user": user,
        "api_version": 3,
        "ignore_auth": True,
    }


def _make_object(job, guid, state, retry_times=0, report_status=None):
    """Create a HarvestObject directly via the ORM.

    The ``harvest_object_create`` action always produces WAITING objects with
    the default retry count, so tests that need objects in a specific state
    build them this way instead of going through the factory.
    """
    obj = HarvestObject(
        guid=guid,
        job=job,
        state=state,
        retry_times=retry_times,
        report_status=report_status,
    )
    obj.save()
    model.Session.commit()
    return HarvestObject.get(obj.id)


@pytest.mark.usefixtures("with_plugins", "clean_db", "clean_queues")
@pytest.mark.ckan_config("ckan.plugins", "harvest test_harvester")
class TestRetryableErrors(object):
    def test_gather_error_defaults_to_permanent(self):
        source = HarvestSourceObj(url="http://permanent.test", source_type="test")
        job = HarvestJobObj(source=source, run=False)

        HarvestGatherError.create("boom", job)

        error = model.Session.query(HarvestGatherError).one()
        assert error.retryable is False
        assert error.message == "boom"

    def test_gather_error_can_be_flagged_retryable(self):
        source = HarvestSourceObj(url="http://retryable.test", source_type="test")
        job = HarvestJobObj(source=source, run=False)

        HarvestGatherError.create("upstream timeout", job, retryable=True)

        error = model.Session.query(HarvestGatherError).one()
        assert error.retryable is True

    def test_object_error_retryable_flag(self):
        source = HarvestSourceObj(url="http://obj.test", source_type="test")
        job = HarvestJobObj(source=source, run=False)
        obj = _make_object(job, "flagged", "ERROR")

        HarvestObjectError.create("permanent validation failure", obj)
        HarvestObjectError.create(
            "connection reset", obj, stage="Fetch", retryable=True
        )

        errors = model.Session.query(HarvestObjectError) \
            .order_by(HarvestObjectError.created).all()
        assert [e.retryable for e in errors] == [False, True]

    def test_transient_error_is_exception(self):
        assert issubclass(TransientHarvestError, Exception)
        with pytest.raises(TransientHarvestError):
            raise TransientHarvestError("503 Service Unavailable")


@pytest.mark.usefixtures("with_plugins", "clean_db", "clean_queues")
@pytest.mark.ckan_config("ckan.plugins", "harvest test_harvester")
class TestRequeueAction(object):
    def setup_method(self):
        if config.get("ckan.harvest.mq.type") != "redis":
            pytest.skip("requeue tests rely on inspecting the Redis queue")
        queue.get_connection().flushdb()

    def teardown_method(self):
        queue.get_connection().flushdb()

    def _job_with_objects(self):
        source = HarvestSourceObj(
            url="http://requeue-action.test", source_type="test"
        )
        job = HarvestJobObj(source=source, run=False)

        retryable_obj = _make_object(
            job, "retryable", "ERROR", retry_times=1
        )
        HarvestObjectError.create(
            "upstream timeout", retryable_obj, stage="Fetch", retryable=True
        )

        permanent_obj = _make_object(
            job, "permanent", "ERROR", retry_times=1
        )
        HarvestObjectError.create("invalid document", permanent_obj)

        complete_obj = _make_object(
            job, "complete", "COMPLETE", report_status="added"
        )

        model.Session.commit()
        return job, retryable_obj, permanent_obj, complete_obj

    def test_only_retryable_error_objects_are_requeued(self):
        job, retryable_obj, permanent_obj, complete_obj = \
            self._job_with_objects()

        result = toolkit.get_action("harvest_objects_requeue")(
            _admin_context(), {"id": job.id}
        )

        assert result["requeued"] == 1
        assert result["not_retryable"] == 1
        assert result["max_retries_exceeded"] == 0

        refreshed = HarvestObject.get(retryable_obj.id)
        assert refreshed.state == "WAITING"
        assert refreshed.report_status is None

        # permanent failure and successful object are untouched
        assert HarvestObject.get(permanent_obj.id).state == "ERROR"
        refreshed_complete = HarvestObject.get(complete_obj.id)
        assert refreshed_complete.state == "COMPLETE"
        assert refreshed_complete.report_status == "added"

        # the object was actually put on the fetch queue
        queued = queue.get_connection().lrange(
            queue.get_fetch_routing_key(), 0, -1
        )
        assert json.loads(queued[0])["harvest_object_id"] == retryable_obj.id

    def test_error_history_is_preserved(self):
        job, retryable_obj, _, _ = self._job_with_objects()
        before = model.Session.query(HarvestObjectError) \
            .filter_by(harvest_object_id=retryable_obj.id).count()
        assert before == 1

        toolkit.get_action("harvest_objects_requeue")(
            _admin_context(), {"id": job.id}
        )

        after = model.Session.query(HarvestObjectError) \
            .filter_by(harvest_object_id=retryable_obj.id).count()
        assert after == 1  # nothing deleted

    def test_objects_over_retry_budget_are_skipped(self):
        source = HarvestSourceObj(url="http://budget.test", source_type="test")
        job = HarvestJobObj(source=source, run=False)
        obj = _make_object(
            job, "exhausted", "ERROR",
            retry_times=queue.DEFAULT_MAX_RETRIES,
        )
        HarvestObjectError.create("still failing", obj, retryable=True)

        result = toolkit.get_action("harvest_objects_requeue")(
            _admin_context(), {"id": job.id}
        )

        assert result["requeued"] == 0
        assert result["max_retries_exceeded"] == 1
        assert HarvestObject.get(obj.id).state == "ERROR"
        assert queue.get_connection().llen(
            queue.get_fetch_routing_key()) == 0

    def test_default_budget_allows_further_attempt(self):
        source = HarvestSourceObj(url="http://custom-budget.test",
                                  source_type="test")
        job = HarvestJobObj(source=source, run=False)
        obj = _make_object(job, "two-strikes", "ERROR", retry_times=2)
        HarvestObjectError.create("timeout", obj, retryable=True)

        result = toolkit.get_action("harvest_objects_requeue")(
            _admin_context(), {"id": job.id}
        )
        # default budget (5) still allows a third attempt
        assert result["requeued"] == 1
        assert result["max_retries"] == queue.DEFAULT_MAX_RETRIES

    def test_already_queued_object_is_not_duplicated(self):
        job, retryable_obj, _, _ = self._job_with_objects()
        queue.get_fetch_publisher().send(
            {"harvest_object_id": retryable_obj.id}
        )

        result = toolkit.get_action("harvest_objects_requeue")(
            _admin_context(), {"id": job.id}
        )

        assert result["requeued"] == 0
        assert result["already_queued"] == 1
        # exactly one message, not two
        assert queue.get_connection().llen(
            queue.get_fetch_routing_key()) == 1

    def test_requeue_is_scoped_to_the_requested_job(self):
        job, retryable_obj, _, _ = self._job_with_objects()

        # another job with its own retryable failure must be left alone
        other_source = HarvestSourceObj(
            url="http://other.test", source_type="test"
        )
        other_job = HarvestJobObj(source=other_source, run=False)
        other_obj = _make_object(other_job, "other", "ERROR")
        HarvestObjectError.create("timeout", other_obj, retryable=True)

        toolkit.get_action("harvest_objects_requeue")(
            _admin_context(), {"id": job.id}
        )

        assert HarvestObject.get(other_obj.id).state == "ERROR"

    def test_requeue_unknown_job_raises(self):
        with pytest.raises(toolkit.ObjectNotFound):
            toolkit.get_action("harvest_objects_requeue")(
                _admin_context(), {"id": "does-not-exist"}
            )

    def test_requeue_gather_error_only_without_objects(self):
        source = HarvestSourceObj(
            url="http://gather-retry.test", source_type="test"
        )
        job = HarvestJobObj(source=source, run=False)
        HarvestGatherError.create("remote 503", job, retryable=True)

        result = toolkit.get_action("harvest_objects_requeue")(
            _admin_context(), {"id": job.id, "include_gather": True}
        )

        assert result["gather_requeued"] is True
        job_obj = HarvestJob.get(job.id)
        assert job_obj.status == "Running"
        queued = queue.get_connection().lrange(
            queue.get_gather_routing_key(), 0, -1
        )
        assert len(queued) == 1

    def test_requeue_gather_refused_when_job_has_objects(self):
        job, _, _, _ = self._job_with_objects()
        HarvestGatherError.create("partial gather failure", job,
                                  retryable=True)

        with pytest.raises(toolkit.ValidationError):
            toolkit.get_action("harvest_objects_requeue")(
                _admin_context(), {"id": job.id, "include_gather": True}
            )

    def test_permanent_gather_error_is_not_requeued(self):
        source = HarvestSourceObj(
            url="http://gather-permanent.test", source_type="test"
        )
        job = HarvestJobObj(source=source, run=False)
        HarvestGatherError.create("bad source config", job)

        result = toolkit.get_action("harvest_objects_requeue")(
            _admin_context(), {"id": job.id, "include_gather": True}
        )

        assert result["gather_requeued"] is False
        assert queue.get_connection().llen(
            queue.get_gather_routing_key()) == 0

    def test_non_sysadmin_cannot_requeue(self):
        job, _, _, _ = self._job_with_objects()
        context = {"model": model, "session": model.Session, "user": None}
        with pytest.raises(toolkit.NotAuthorized):
            toolkit.get_action("harvest_objects_requeue")(
                context, {"id": job.id}
            )


@pytest.mark.usefixtures("with_plugins", "clean_db", "clean_queues")
@pytest.mark.ckan_config("ckan.plugins", "harvest test_harvester")
class TestFetchCallbackRetryBudget(object):
    def setup_method(self):
        if config.get("ckan.harvest.mq.type") != "redis":
            pytest.skip()
        queue.get_connection().flushdb()

    def teardown_method(self):
        queue.get_connection().flushdb()

    def test_object_hitting_budget_is_errored_and_acked(self):
        source = HarvestSourceObj(url="http://budget-callback.test",
                                  source_type="test")
        job = HarvestJobObj(source=source, run=False)
        obj = _make_object(
            job, "loop", "WAITING",
            retry_times=queue.DEFAULT_MAX_RETRIES - 1,
        )
        queue.get_fetch_publisher().send({"harvest_object_id": obj.id})

        consumer = queue.get_fetch_consumer()
        method, _, body = consumer.basic_get(
            queue=queue.get_fetch_queue_name()
        )
        queue.fetch_callback(consumer, method, None, body)

        refreshed = HarvestObject.get(obj.id)
        assert refreshed.state == "ERROR"
        assert refreshed.retry_times == queue.DEFAULT_MAX_RETRIES
        # message was acknowledged, nothing left to redeliver
        assert queue.get_connection().llen(
            queue.get_fetch_routing_key()) == 0

    @pytest.mark.ckan_config("ckan.harvest.max_retries", "2")
    def test_configurable_retry_budget(self):
        assert queue.get_max_retries() == 2
        source = HarvestSourceObj(url="http://budget-two.test",
                                  source_type="test")
        job = HarvestJobObj(source=source, run=False)
        obj = _make_object(job, "loop2", "WAITING", retry_times=1)
        queue.get_fetch_publisher().send(
            {"harvest_object_id": obj.id}
        )

        consumer = queue.get_fetch_consumer()
        method, _, body = consumer.basic_get(
            queue=queue.get_fetch_queue_name()
        )
        queue.fetch_callback(consumer, method, None, body)

        refreshed = HarvestObject.get(obj.id)
        assert refreshed.state == "ERROR"
        assert refreshed.retry_times == 2


@pytest.mark.usefixtures("with_plugins", "clean_db", "clean_queues")
@pytest.mark.ckan_config("ckan.plugins", "harvest test_harvester")
class TestTransientGatherStage(object):
    def test_transient_gather_error_keeps_gathered_objects(self):
        from ckanext.harvest.interfaces import IHarvester
        from ckan.plugins.core import SingletonPlugin, implements

        source = HarvestSourceObj(
            url="http://transient-gather.test", source_type="test"
        )
        job = HarvestJobObj(source=source, run=False)

        # An object created before the transient failure must survive.
        early_obj = _make_object(job, "early", "WAITING")

        class ExplodingHarvester(SingletonPlugin):
            implements(IHarvester)

            def info(self):
                return {"name": "test", "title": "t", "description": "t"}

            def gather_stage(self, harvest_job):
                raise TransientHarvestError("upstream 502")

            def fetch_stage(self, harvest_object):
                return True

            def import_stage(self, harvest_object):
                return True

        harvester = ExplodingHarvester()
        ids = queue.gather_stage(harvester, HarvestJob.get(job.id))

        assert early_obj.id in ids
        error = model.Session.query(HarvestGatherError).one()
        assert error.retryable is True
        # the partially gathered object was not deleted
        assert model.Session.query(HarvestObject) \
            .filter_by(id=early_obj.id).count() == 1


@pytest.mark.usefixtures("with_plugins", "clean_db", "clean_queues")
@pytest.mark.ckan_config("ckan.plugins", "harvest test_harvester")
class TestTransientFetchAndImport(object):
    def _harvester(self, failing_stage):
        from ckanext.harvest.interfaces import IHarvester
        from ckan.plugins.core import SingletonPlugin, implements

        class FlakyHarvester(SingletonPlugin):
            implements(IHarvester)

            def info(self):
                return {"name": "test", "title": "t", "description": "t"}

            def gather_stage(self, harvest_job):
                return []

            def fetch_stage(self, harvest_object):
                if failing_stage == "fetch":
                    raise TransientHarvestError("fetch timeout")
                return True

            def import_stage(self, harvest_object):
                if failing_stage == "import":
                    raise TransientHarvestError("import 503")
                return True

        return FlakyHarvester()

    @pytest.mark.parametrize("stage", ["fetch", "import"])
    def test_transient_failure_recorded_as_retryable(self, stage):
        source = HarvestSourceObj(
            url="http://flaky-%s.test" % stage, source_type="test"
        )
        job = HarvestJobObj(source=source, run=False)
        obj = _make_object(job, "flaky", "WAITING")

        queue.fetch_and_import_stages(self._harvester(stage), obj)

        refreshed = HarvestObject.get(obj.id)
        assert refreshed.state == "ERROR"
        assert refreshed.report_status == "errored"
        error = model.Session.query(HarvestObjectError).one()
        assert error.retryable is True
        assert stage.capitalize() == error.stage

    def test_retryable_object_can_recover_on_a_later_attempt(self):
        source = HarvestSourceObj(
            url="http://recover.test", source_type="test"
        )
        job = HarvestJobObj(source=source, run=False)
        obj = _make_object(job, "recover", "WAITING")

        # first attempt: transient fetch failure
        queue.fetch_and_import_stages(
            self._harvester("fetch"), obj
        )
        assert HarvestObject.get(obj.id).state == "ERROR"

        # operator requeues it within the retry budget
        result = toolkit.get_action("harvest_objects_requeue")(
            _admin_context(), {"id": job.id}
        )
        assert result["requeued"] == 1
        assert HarvestObject.get(obj.id).state == "WAITING"

        # a healthy harvester then completes the object; original error
        # history is still present
        from ckanext.harvest.interfaces import IHarvester
        from ckan.plugins.core import SingletonPlugin, implements

        class HealthyHarvester(SingletonPlugin):
            implements(IHarvester)

            def info(self):
                return {"name": "test", "title": "t", "description": "t"}

            def gather_stage(self, harvest_job):
                return []

            def fetch_stage(self, harvest_object):
                harvest_object.content = "{}"
                return True

            def import_stage(self, harvest_object):
                harvest_object.current = True
                return True

        queue.fetch_and_import_stages(HealthyHarvester(),
                                      HarvestObject.get(obj.id))
        refreshed = HarvestObject.get(obj.id)
        assert refreshed.state == "COMPLETE"
        assert model.Session.query(HarvestObjectError).count() == 1


@pytest.mark.usefixtures("with_plugins", "clean_db", "clean_queues")
@pytest.mark.ckan_config("ckan.plugins", "harvest ckan_harvester")
class TestCkanHarvesterTransientClassification(object):
    def _harvester(self):
        from ckanext.harvest.harvesters.ckanharvester import CKANHarvester
        h = CKANHarvester()
        h.config = {}
        return h

    def _mock_response(self, status_code):
        from unittest.mock import MagicMock
        response = MagicMock()
        response.status_code = status_code
        return response

    def test_5xx_is_classified_as_transient(self):
        from ckanext.harvest.harvesters.ckanharvester import (
            TransientContentFetchError,
        )
        harvester = self._harvester()
        with patch(
            "ckanext.harvest.harvesters.ckanharvester.requests.get",
            return_value=self._mock_response(503),
        ):
            with pytest.raises(TransientContentFetchError):
                harvester._get_content("http://upstream/x")

    def test_429_is_classified_as_transient(self):
        from ckanext.harvest.harvesters.ckanharvester import (
            TransientContentFetchError,
        )
        harvester = self._harvester()
        with patch(
            "ckanext.harvest.harvesters.ckanharvester.requests.get",
            return_value=self._mock_response(429),
        ):
            with pytest.raises(TransientContentFetchError):
                harvester._get_content("http://upstream/x")

    def test_404_remains_a_permanent_content_error(self):
        from ckanext.harvest.harvesters.ckanharvester import (
            ContentFetchError,
            TransientContentFetchError,
        )
        import requests as requests_lib
        from unittest.mock import MagicMock

        harvester = self._harvester()
        response = self._mock_response(404)

        # A real requests HTTPError carries the originating response and
        # request objects; mimic that so the production error formatting works.
        http_error = requests_lib.exceptions.HTTPError("404 Client Error")
        http_error.response = MagicMock(status_code=404)
        http_error.request = MagicMock(url="http://upstream/missing")
        response.raise_for_status = MagicMock(side_effect=http_error)
        with patch(
            "ckanext.harvest.harvesters.ckanharvester.requests.get",
            return_value=response,
        ):
            with pytest.raises(ContentFetchError) as exc_info:
                harvester._get_content("http://upstream/missing")
            assert not isinstance(exc_info.value,
                                  TransientContentFetchError)

    def test_connection_error_is_classified_as_transient(self):
        from requests.exceptions import ConnectionError
        from ckanext.harvest.harvesters.ckanharvester import (
            TransientContentFetchError,
        )
        harvester = self._harvester()
        with patch(
            "ckanext.harvest.harvesters.ckanharvester.requests.get",
            side_effect=ConnectionError("reset by peer"),
        ):
            with pytest.raises(TransientContentFetchError):
                harvester._get_content("http://upstream/x")

    def test_retryable_gather_error_on_search_failure(self):
        """A SearchError during the full-fallback search must be stored as a
        retryable gather error so the job can be recovered."""
        from ckanext.harvest.harvesters.ckanharvester import SearchError
        from unittest.mock import patch as _patch

        source = HarvestSourceObj(
            url="http://gather-flaky.test", source_type="ckan"
        )
        job = HarvestJobObj(source=source, run=False)
        harvester = self._harvester()

        with _patch.object(
            harvester, "_search_for_datasets",
            side_effect=SearchError("upstream 500"),
        ):
            result = harvester.gather_stage(HarvestJob.get(job.id))

        assert result is None
        error = model.Session.query(HarvestGatherError).one()
        assert error.retryable is True
        assert queue.job_has_retryable_gather_errors(
            HarvestJob.get(job.id)) is True
