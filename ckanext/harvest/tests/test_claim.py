'''Tests for atomic fetch claims on harvest objects.

These exercise the queue-level claim behaviour (atomic claim, duplicate
delivery, lease expiry / takeover and fenced result writes) without needing a
real Redis/RabbitMQ broker: the callbacks operate directly on the database
like the existing test_queue.py tests do.
'''
import datetime
import json
import time
import uuid

import pytest

from ckan import model

import ckanext.harvest.model as harvest_model
from ckanext.harvest.tests import factories
from ckanext.harvest import queue


class FakeChannel(object):
    '''Records acks so tests can assert whether a message was acknowledged.'''

    def __init__(self):
        self.acks = []

    def basic_ack(self, delivery_tag):
        self.acks.append(delivery_tag)

    def close(self):
        pass


class FakeMethod(object):
    def __init__(self, body):
        self.delivery_tag = body


def _make_source_and_object(state='WAITING'):
    unique = uuid.uuid4().hex
    source = factories.HarvestSourceObj(
        url='http://claim-{0}.test.com'.format(unique),
        source_type='test-for-action',
    )
    job = factories.HarvestJobObj(source=source, run=False)
    job.status = 'Running'
    job.save()
    obj = factories.HarvestObjectObj(
        job=job,
        source=source,
        guid='claim-guid-{0}'.format(unique),
        state=state,
    )
    obj.state = state
    obj.save()
    return source, job, obj


@pytest.mark.usefixtures('with_plugins', 'clean_db', 'clean_queues')
@pytest.mark.ckan_config('ckan.plugins', 'harvest test_action_harvester')
class TestAtomicClaim(object):

    def _reload(self, obj_id):
        model.Session.remove()
        return harvest_model.HarvestObject.get(obj_id)

    def test_claim_is_exclusive(self):
        _, _, obj = _make_source_and_object()

        first = harvest_model.HarvestObject.claim_for(obj.id, 'token-a', 60)
        second = harvest_model.HarvestObject.claim_for(obj.id, 'token-b', 60)

        assert first is True
        assert second is False

        reloaded = self._reload(obj.id)
        assert reloaded.claim_token == 'token-a'

    def test_claim_can_be_renewed_by_holder(self):
        _, _, obj = _make_source_and_object()
        assert harvest_model.HarvestObject.claim_for(obj.id, 'token-a', 60)
        assert harvest_model.HarvestObject.renew_claim_for(
            obj.id, 'token-a', 60)

    def test_claim_cannot_be_renewed_by_other(self):
        _, _, obj = _make_source_and_object()
        assert harvest_model.HarvestObject.claim_for(obj.id, 'token-a', 60)
        assert not harvest_model.HarvestObject.renew_claim_for(
            obj.id, 'token-b', 60)

    def test_released_claim_can_be_acquired(self):
        _, _, obj = _make_source_and_object()
        assert harvest_model.HarvestObject.claim_for(obj.id, 'token-a', 60)
        assert harvest_model.HarvestObject.release_claim_for(obj.id, 'token-a')
        assert harvest_model.HarvestObject.claim_for(obj.id, 'token-b', 60)

    def test_only_holder_can_release(self):
        _, _, obj = _make_source_and_object()
        assert harvest_model.HarvestObject.claim_for(obj.id, 'token-a', 60)
        assert not harvest_model.HarvestObject.release_claim_for(
            obj.id, 'token-b')
        reloaded = self._reload(obj.id)
        assert reloaded.claim_token == 'token-a'

    def test_expired_claim_can_be_taken_over(self):
        _, _, obj = _make_source_and_object()
        assert harvest_model.HarvestObject.claim_for(obj.id, 'token-a', 60)

        # Force the lease into the past, as if the holder died.
        obj = self._reload(obj.id)
        obj.claim_expires = datetime.datetime.utcnow() - datetime.timedelta(
            seconds=5)
        obj.save()

        assert harvest_model.HarvestObject.claim_for(obj.id, 'token-b', 60)
        reloaded = self._reload(obj.id)
        assert reloaded.claim_token == 'token-b'

    def test_fenced_write_succeeds_for_holder(self):
        _, _, obj = _make_source_and_object()
        assert harvest_model.HarvestObject.claim_for(obj.id, 'token-a', 60)
        assert harvest_model.HarvestObject.save_claim_guarded_for(
            obj.id, 'token-a', state='COMPLETE', report_status='added')

        reloaded = self._reload(obj.id)
        assert reloaded.state == 'COMPLETE'
        assert reloaded.report_status == 'added'

    def test_fenced_write_rejected_for_stale_worker(self):
        _, _, obj = _make_source_and_object()
        # Worker A claims, then its lease expires and B takes over.
        assert harvest_model.HarvestObject.claim_for(obj.id, 'token-a', 60)
        obj = self._reload(obj.id)
        obj.claim_expires = datetime.datetime.utcnow() - datetime.timedelta(
            seconds=5)
        obj.save()
        assert harvest_model.HarvestObject.claim_for(obj.id, 'token-b', 60)

        # B writes its result.
        assert harvest_model.HarvestObject.save_claim_guarded_for(
            obj.id, 'token-b', state='COMPLETE', report_status='updated')

        # A finally finishes and tries to write - it must be rejected and must
        # not overwrite B's result.
        assert not harvest_model.HarvestObject.save_claim_guarded_for(
            obj.id, 'token-a', state='ERROR', report_status='errored')

        reloaded = self._reload(obj.id)
        assert reloaded.state == 'COMPLETE'
        assert reloaded.report_status == 'updated'


@pytest.mark.usefixtures('with_plugins', 'clean_db', 'clean_queues')
@pytest.mark.ckan_config('ckan.plugins', 'harvest test_action_harvester')
class TestFetchCallbackClaims(object):

    def _body(self, obj_id):
        return json.dumps({'harvest_object_id': obj_id})

    def _fetch(self, obj_id):
        channel = FakeChannel()
        body = self._body(obj_id)
        queue.fetch_callback(channel, FakeMethod(body), None, body)
        return channel

    def _reload(self, obj_id):
        model.Session.remove()
        return harvest_model.HarvestObject.get(obj_id)

    def test_completed_object_is_acked_without_reprocessing(self):
        _, _, obj = _make_source_and_object(state='COMPLETE')
        # Simulate a retry counter that would trip ERROR if reprocessed.
        obj.retry_times = 4
        obj.save()

        channel = self._fetch(obj.id)

        assert channel.acks, 'duplicate message for a finished object should be acked'
        reloaded = self._reload(obj.id)
        assert reloaded.retry_times == 4
        assert reloaded.state == 'COMPLETE'

    def test_errored_object_is_acked_without_reprocessing(self):
        _, _, obj = _make_source_and_object(state='ERROR')
        channel = self._fetch(obj.id)
        assert channel.acks

    def test_duplicate_delivery_does_not_double_process(self):
        _, _, obj = _make_source_and_object()

        channel1 = self._fetch(obj.id)
        reloaded = self._reload(obj.id)
        # The shared test harvester leaves the object COMPLETE.
        assert reloaded.state == 'COMPLETE'
        assert reloaded.retry_times == 1
        assert channel1.acks

        # A second delivery of the same message must be acked and left alone.
        channel2 = self._fetch(obj.id)
        assert channel2.acks
        reloaded = self._reload(obj.id)
        assert reloaded.retry_times == 1
        assert reloaded.state == 'COMPLETE'
        assert reloaded.claim_token is None

    def test_object_claimed_by_another_worker_is_not_processed(self):
        _, _, obj = _make_source_and_object()
        object_id = obj.id
        # Another live worker holds the claim.
        assert harvest_model.HarvestObject.claim_for(object_id, 'other-worker', 600)

        channel = self._fetch(object_id)

        # Not acknowledged - it should be retried once the holder is done or
        # its lease expires.
        assert channel.acks == []
        reloaded = self._reload(object_id)
        assert reloaded.state == 'WAITING'
        assert reloaded.retry_times == 0
        assert reloaded.claim_token == 'other-worker'

    def test_expired_claim_is_taken_over(self):
        _, _, obj = _make_source_and_object()
        # Stale claim from a worker that died.
        obj.claim_token = 'dead-worker'
        obj.claim_expires = datetime.datetime.utcnow() - datetime.timedelta(
            seconds=30)
        obj.save()

        channel = self._fetch(obj.id)

        assert channel.acks
        reloaded = self._reload(obj.id)
        assert reloaded.state == 'COMPLETE'
        assert reloaded.claim_token is None


@pytest.mark.usefixtures('with_plugins', 'clean_db', 'clean_queues')
@pytest.mark.ckan_config('ckan.plugins', 'harvest test_action_harvester')
class TestStaleWorkerCannotOverwrite(object):
    '''A worker whose lease expired must not write over the new holder.'''

    def _reload(self, obj_id):
        model.Session.remove()
        return harvest_model.HarvestObject.get(obj_id)

    def test_stages_from_stale_worker_are_discarded(self):
        _, _, obj = _make_source_and_object()
        obj_id = obj.id

        # Worker A claims the object.
        assert harvest_model.HarvestObject.claim_for(obj_id, 'worker-a', 60)

        # A stalls; its lease lapses and worker B takes over and completes.
        stale = self._reload(obj_id)
        stale.claim_expires = datetime.datetime.utcnow() - datetime.timedelta(
            seconds=5)
        stale.save()
        assert harvest_model.HarvestObject.claim_for(obj_id, 'worker-b', 60)
        assert harvest_model.HarvestObject.save_claim_guarded_for(
            obj_id, 'worker-b', state='COMPLETE', report_status='updated')

        # A wakes up and runs the stages with its stale token. Every write must
        # be rejected, so B's result survives.
        harvester = _StubHarvester()
        result = queue.fetch_and_import_stages(
            harvester, self._reload(obj_id), token='worker-a')

        assert result is False
        reloaded = self._reload(obj_id)
        assert reloaded.state == 'COMPLETE'
        assert reloaded.report_status == 'updated'

    def test_heartbeat_renews_a_live_claim(self):
        _, _, obj = _make_source_and_object()
        obj_id = obj.id
        assert harvest_model.HarvestObject.claim_for(obj_id, 'worker-a', 3)

        # Give the initial lease an almost-expired deadline, then let the
        # heartbeat run a couple of intervals.
        reloaded = self._reload(obj_id)
        reloaded.claim_expires = datetime.datetime.utcnow() + datetime.timedelta(
            seconds=1)
        reloaded.save()

        heartbeat = queue.ClaimHeartbeat(obj_id, 'worker-a', 3)
        with heartbeat:
            time.sleep(2.5)

        assert heartbeat.lost is False
        still_owned = harvest_model.HarvestObject.renew_claim_for(
            obj_id, 'worker-a', 60)
        assert still_owned


@pytest.mark.usefixtures('with_plugins', 'clean_db', 'clean_queues')
@pytest.mark.ckan_config('ckan.plugins', 'harvest test_action_harvester')
class TestResubmitRespectsClaims(object):

    def test_resubmit_objects_skips_live_claimed_object(self):
        if queue.config.get('ckan.harvest.mq.type') != 'redis':
            pytest.skip('resubmit_objects only runs on the redis backend')

        from ckanext.harvest import queue as q

        redis = q.get_connection()
        redis.flushdb()
        try:
            # A WAITING object that is not in the queue would normally be
            # resubmitted...
            _, _, claimed = _make_source_and_object()
            assert harvest_model.HarvestObject.claim_for(
                claimed.id, 'worker-a', 600)

            # ...and one that is not claimed should be resubmitted.
            _, _, free = _make_source_and_object()

            q.resubmit_objects()

            queued = [json.loads(m)['harvest_object_id']
                      for m in redis.lrange(q.get_fetch_routing_key(), 0, -1)]
            assert free.id in queued
            assert claimed.id not in queued
        finally:
            redis.flushdb()


class _StubHarvester(object):
    def info(self):
        return {'name': 'test-for-action'}

    def fetch_stage(self, harvest_object):
        return True

    def import_stage(self, harvest_object):
        return True
