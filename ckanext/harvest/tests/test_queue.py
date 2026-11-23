import pytest
try:
    from unittest.mock import patch, Mock
except ImportError:
    from mock import patch, Mock

import datetime

from ckanext.harvest.model import HarvestObject, HarvestObjectExtra
from ckanext.harvest.interfaces import IHarvester
from ckanext.harvest.tests import factories as harvest_factories
import ckanext.harvest.queue as queue
from ckan.plugins.core import SingletonPlugin, implements
import json
from ckan.plugins import toolkit
from ckan import model
from ckan.lib.base import config
import uuid


class MockHarvester(SingletonPlugin):
    implements(IHarvester)

    def info(self):
        return {'name': 'test', 'title': 'test', 'description': 'test'}

    def gather_stage(self, harvest_job):

        if harvest_job.source.url.startswith('basic_test'):
            obj = HarvestObject(guid='test1', job=harvest_job)
            obj.extras.append(HarvestObjectExtra(key='key', value='value'))
            obj2 = HarvestObject(guid='test2', job=harvest_job)
            obj3 = HarvestObject(guid='test_to_delete', job=harvest_job)
            obj.add()
            obj2.add()
            obj3.save()  # this will commit both
            return [obj.id, obj2.id, obj3.id]

        return []

    def fetch_stage(self, harvest_object):
        assert harvest_object.state == "FETCH"
        assert harvest_object.fetch_started is not None
        harvest_object.content = json.dumps({'name': harvest_object.guid})
        harvest_object.save()
        return True

    def import_stage(self, harvest_object):
        assert harvest_object.state == "IMPORT"
        assert harvest_object.fetch_finished is not None
        assert harvest_object.import_started is not None

        user = toolkit.get_action('get_site_user')(
            {'model': model, 'ignore_auth': True}, {}
        )['name']

        package = json.loads(harvest_object.content)
        name = package['name']

        package_object = model.Package.get(name)
        if package_object:
            logic_function = 'package_update'
        else:
            logic_function = 'package_create'

        package_dict = toolkit.get_action(logic_function)(
            {'model': model, 'session': model.Session,
             'user': user, 'api_version': 3, 'ignore_auth': True},
            json.loads(harvest_object.content)
        )

        # set previous objects to not current
        previous_object = model.Session.query(HarvestObject) \
            .filter(HarvestObject.guid == harvest_object.guid) \
            .filter(
            HarvestObject.current == True  # noqa: E712
        ).first()
        if previous_object:
            previous_object.current = False
            previous_object.save()

        # delete test_to_delete package on second run
        harvest_object.package_id = package_dict['id']
        harvest_object.current = True
        if package_dict['name'] == 'test_to_delete' and package_object:
            harvest_object.current = False
            package_object.state = 'deleted'
            package_object.save()

        harvest_object.save()
        return True


@pytest.mark.usefixtures('with_plugins', 'clean_db', 'clean_queues')
@pytest.mark.ckan_config('ckan.plugins', 'harvest test_harvester')
class TestHarvestQueue(object):

    def test_01_basic_harvester(self):

        if config.get('ckan.harvest.mq.type') == 'redis':
            # make sure that there are no old elements in the redis db
            redis = queue.get_connection()
            redis.flushdb()

        # make sure queues/exchanges are created first and are empty
        consumer = queue.get_gather_consumer()
        consumer_fetch = queue.get_fetch_consumer()
        consumer.queue_purge(queue=queue.get_gather_queue_name())
        consumer_fetch.queue_purge(queue=queue.get_fetch_queue_name())

        user = toolkit.get_action('get_site_user')(
            {'model': model, 'ignore_auth': True}, {}
        )['name']

        context = {'model': model, 'session': model.Session,
                   'user': user, 'api_version': 3, 'ignore_auth': True}

        harvest_source, job_id = self._create_harvest_job_and_finish_gather_stage(consumer, context)

        # do three times as three harvest objects
        reply = consumer_fetch.basic_get(queue='ckan.harvest.fetch')
        queue.fetch_callback(consumer_fetch, *reply)
        reply = consumer_fetch.basic_get(queue='ckan.harvest.fetch')
        queue.fetch_callback(consumer_fetch, *reply)
        reply = consumer_fetch.basic_get(queue='ckan.harvest.fetch')
        queue.fetch_callback(consumer_fetch, *reply)

        count = model.Session.query(model.Package) \
            .filter(model.Package.type == 'dataset') \
            .count()
        assert count == 3
        all_objects = model.Session.query(HarvestObject).filter_by(current=True).all()

        assert len(all_objects) == 3
        assert all_objects[0].state == 'COMPLETE'
        assert all_objects[0].report_status == 'added'
        assert all_objects[1].state == 'COMPLETE'
        assert all_objects[1].report_status == 'added'
        assert all_objects[2].state == 'COMPLETE'
        assert all_objects[2].report_status == 'added'

        # fire run again to check if job is set to Finished
        toolkit.get_action('harvest_jobs_run')(
            context,
            {'source_id': harvest_source['id']}
        )

        harvest_job = toolkit.get_action('harvest_job_show')(
            context,
            {'id': job_id}
        )

        assert harvest_job['status'] == u'Finished'
        assert harvest_job['stats'] == {'added': 3, 'updated': 0, 'not modified': 0, 'errored': 0, 'deleted': 0}

        harvest_source_dict = toolkit.get_action('harvest_source_show')(
            context,
            {'id': harvest_source['id']}
        )

        assert harvest_source_dict['status']['last_job']['stats'] == {
            'added': 3, 'updated': 0, 'not modified': 0, 'errored': 0, 'deleted': 0}
        assert harvest_source_dict['status']['total_datasets'] == 3
        assert harvest_source_dict['status']['job_count'] == 1

        # Second run
        harvest_job = toolkit.get_action('harvest_job_create')(
            context,
            {'source_id': harvest_source['id'], 'run': True}
        )

        job_id = harvest_job['id']
        assert toolkit.get_action('harvest_job_show')(
            context,
            {'id': job_id}
        )['status'] == u'Running'

        # pop on item off the queue and run the callback
        reply = consumer.basic_get(queue='ckan.harvest.gather')
        queue.gather_callback(consumer, *reply)

        all_objects = model.Session.query(HarvestObject).all()

        assert len(all_objects) == 6

        reply = consumer_fetch.basic_get(queue='ckan.harvest.fetch')
        queue.fetch_callback(consumer_fetch, *reply)
        reply = consumer_fetch.basic_get(queue='ckan.harvest.fetch')
        queue.fetch_callback(consumer_fetch, *reply)
        reply = consumer_fetch.basic_get(queue='ckan.harvest.fetch')
        queue.fetch_callback(consumer_fetch, *reply)

        count = model.Session.query(model.Package) \
            .filter(model.Package.type == 'dataset') \
            .count()
        assert count == 3

        all_objects = model.Session.query(HarvestObject).filter_by(report_status='added').all()
        assert len(all_objects) == 3

        all_objects = model.Session.query(HarvestObject).filter_by(report_status='updated').all()
        assert len(all_objects) == 2

        all_objects = model.Session.query(HarvestObject).filter_by(report_status='deleted').all()
        assert len(all_objects) == 1

        # run to make sure job is marked as finshed
        toolkit.get_action('harvest_jobs_run')(
            context,
            {'source_id': harvest_source['id']}
        )

        harvest_job = toolkit.get_action('harvest_job_show')(
            context,
            {'id': job_id}
        )
        assert harvest_job['stats'] == {'added': 0, 'updated': 2, 'not modified': 0, 'errored': 0, 'deleted': 1}

        harvest_source_dict = toolkit.get_action('harvest_source_show')(
            context,
            {'id': harvest_source['id']}
        )

        assert harvest_source_dict['status']['last_job']['stats'] == {
            'added': 0, 'updated': 2, 'not modified': 0, 'errored': 0, 'deleted': 1}
        assert harvest_source_dict['status']['total_datasets'] == 2
        assert harvest_source_dict['status']['job_count'] == 2

    def test_redis_queue_purging(self):
        '''
        Test that Redis queue purging doesn't purge the wrong keys.
        '''
        if config.get('ckan.harvest.mq.type') != 'redis':
            pytest.skip()
        redis = queue.get_connection()
        try:
            redis.set('ckanext-harvest:some-random-key', 'foobar')

            # Create some fake jobs
            gather_publisher = queue.get_gather_publisher()
            gather_publisher.send({'harvest_job_id': str(uuid.uuid4())})
            gather_publisher.send({'harvest_job_id': str(uuid.uuid4())})
            fetch_publisher = queue.get_fetch_publisher()
            fetch_publisher.send({'harvest_object_id': str(uuid.uuid4())})
            fetch_publisher.send({'harvest_object_id': str(uuid.uuid4())})
            num_keys = redis.dbsize()

            # Create some fake objects
            gather_consumer = queue.get_gather_consumer()
            next(gather_consumer.consume(queue.get_gather_queue_name()))
            fetch_consumer = queue.get_fetch_consumer()
            next(fetch_consumer.consume(queue.get_fetch_queue_name()))

            assert redis.dbsize() > num_keys

            queue.purge_queues()

            assert redis.get('ckanext-harvest:some-random-key') == 'foobar'
            assert redis.dbsize() == num_keys
            assert redis.llen(queue.get_gather_routing_key()) == 0
            assert redis.llen(queue.get_fetch_routing_key()) == 0
        finally:
            redis.delete('ckanext-harvest:some-random-key')

    def test_resubmit_objects(self):
        '''
        Test that only harvest objects re-submitted which were not be present in the redis fetch queue.
        '''
        if config.get('ckan.harvest.mq.type') != 'redis':
            pytest.skip()
        # make sure that there are no old elements in the redis db
        redis = queue.get_connection()
        fetch_routing_key = queue.get_fetch_routing_key()
        redis.flushdb()
        try:
            # make sure queues/exchanges are created first and are empty
            consumer = queue.get_gather_consumer()
            consumer_fetch = queue.get_fetch_consumer()
            consumer.queue_purge(queue=queue.get_gather_queue_name())
            consumer_fetch.queue_purge(queue=queue.get_fetch_queue_name())

            user = toolkit.get_action('get_site_user')(
                {'model': model, 'ignore_auth': True}, {}
            )['name']

            context = {'model': model, 'session': model.Session,
                       'user': user, 'api_version': 3, 'ignore_auth': True}

            harvest_source, job_id = self._create_harvest_job_and_finish_gather_stage(consumer, context)

            assert redis.llen(fetch_routing_key) == 3

            # do only one time for the first harvest object
            reply = consumer_fetch.basic_get(queue='ckan.harvest.fetch')
            queue.fetch_callback(consumer_fetch, *reply)

            count = model.Session.query(model.Package) \
                .filter(model.Package.type == 'dataset') \
                .count()
            assert count == 1

            all_objects = model.Session.query(HarvestObject).order_by(HarvestObject.state.asc()).all()
            assert len(all_objects) == 3
            assert all_objects[0].state == 'COMPLETE'
            assert all_objects[0].report_status == 'added'
            assert all_objects[0].current is True
            assert all_objects[1].state == 'WAITING'
            assert all_objects[1].current is False
            assert all_objects[2].state == 'WAITING'
            assert all_objects[2].current is False

            assert len(redis.keys(fetch_routing_key + ':*')) == 0
            assert redis.llen(fetch_routing_key) == 2

            # Remove one object from redis that should be re-sent to the fetch queue
            reply = consumer_fetch.basic_get(queue='ckan.harvest.fetch')
            fetch_queue_items = redis.lrange(fetch_routing_key, 0, 10)
            assert len(fetch_queue_items) == 1
            harvest_object_id = reply[2]
            assert fetch_queue_items[0] != harvest_object_id

            queue.resubmit_objects()

            assert redis.llen(fetch_routing_key) == 2
            fetch_queue_items = redis.lrange(fetch_routing_key, 0, 10)
            assert harvest_object_id in fetch_queue_items
            assert redis.dbsize() == 1
        finally:
            redis.flushdb()

    @pytest.mark.ckan_config('ckan.harvest.fetch_queue.max_objects_per_batch', 2)
    def test_gather_sends_objects_in_limited_batches(self):
        '''
        After a gather, only a first limited batch of the waiting objects
        is sent to the fetch queue. The next harvest_jobs_run call sends
        the following batch.
        '''
        if config.get('ckan.harvest.mq.type') != 'redis':
            pytest.skip()
        redis = queue.get_connection()
        fetch_routing_key = queue.get_fetch_routing_key()
        redis.flushdb()
        try:
            consumer = queue.get_gather_consumer()
            consumer_fetch = queue.get_fetch_consumer()
            consumer.queue_purge(queue=queue.get_gather_queue_name())
            consumer_fetch.queue_purge(queue=queue.get_fetch_queue_name())

            user = toolkit.get_action('get_site_user')(
                {'model': model, 'ignore_auth': True}, {}
            )['name']

            context = {'model': model, 'session': model.Session,
                       'user': user, 'api_version': 3, 'ignore_auth': True}

            harvest_source, job_id = \
                self._create_harvest_job_and_finish_gather_stage(consumer, context)

            # The gather created 3 objects, but only the first batch of
            # 2 was sent to the fetch queue
            assert redis.llen(fetch_routing_key) == 2
            assert model.Session.query(HarvestObject) \
                .filter_by(state='WAITING') \
                .filter(HarvestObject.fetch_queued.isnot(None)) \
                .count() == 2

            # The next harvest_jobs_run call sends the following batch
            toolkit.get_action('harvest_jobs_run')(
                context,
                {'source_id': harvest_source['id']}
            )

            assert redis.llen(fetch_routing_key) == 3
            assert model.Session.query(HarvestObject) \
                .filter_by(state='WAITING') \
                .filter(HarvestObject.fetch_queued.is_(None)) \
                .count() == 0
        finally:
            redis.flushdb()

    def _create_harvest_job_and_finish_gather_stage(self, consumer, context):
        source_dict = {'title': 'Test Source',
                       'name': 'test-source',
                       'url': 'basic_test',
                       'source_type': 'test'}

        try:
            harvest_source = toolkit.get_action('harvest_source_create')(
                context,
                source_dict)
        except toolkit.ValidationError:
            harvest_source = toolkit.get_action('harvest_source_show')(
                context,
                {'id': source_dict['name']}
            )
            pass

        assert harvest_source['source_type'] == 'test', harvest_source
        assert harvest_source['url'] == 'basic_test', harvest_source

        harvest_job = toolkit.get_action('harvest_job_create')(
            context,
            {'source_id': harvest_source['id'], 'run': True})
        job_id = harvest_job['id']

        assert harvest_job['source_id'] == harvest_source['id'], harvest_job
        assert harvest_job['status'] == u'Running'

        assert toolkit.get_action('harvest_job_show')(
            context,
            {'id': job_id}
        )['status'] == u'Running'

        # pop on item off the queue and run the callback
        reply = consumer.basic_get(queue='ckan.harvest.gather')

        queue.gather_callback(consumer, *reply)

        all_objects = model.Session.query(HarvestObject).all()

        assert len(all_objects) == 3
        assert all_objects[0].state == 'WAITING'
        assert all_objects[1].state == 'WAITING'
        assert all_objects[2].state == 'WAITING'

        assert len(model.Session.query(HarvestObject).all()) == 3
        assert len(model.Session.query(HarvestObjectExtra).all()) == 1

        return harvest_source, job_id


@pytest.mark.usefixtures('with_plugins', 'clean_db', 'clean_queues')
@pytest.mark.ckan_config('ckan.plugins', 'harvest test_harvester')
class TestSendWaitingObjectsToFetchQueue(object):
    '''
    Tests for the fair, quota limited dispatch of waiting harvest
    objects to the fetch queue.
    '''

    def setup_method(self):
        if config.get('ckan.harvest.mq.type') != 'redis':
            pytest.skip('These tests need the redis backend')
        queue.get_connection().flushdb()

    def teardown_method(self):
        queue.get_connection().flushdb()

    def _fetch_queue_object_ids(self):
        redis = queue.get_connection()
        return [
            json.loads(o)['harvest_object_id']
            for o in redis.lrange(queue.get_fetch_routing_key(), 0, -1)
        ]

    def _make_source_with_objects(self, num_objects):
        source = harvest_factories.HarvestSourceObj(source_type='test')
        job = harvest_factories.HarvestJobObj(source=source)
        objects = []
        for i in range(num_objects):
            obj = harvest_factories.HarvestObjectObj(
                job=job, guid='obj-{}-{}'.format(source.id, i))
            # Make the source's own object order explicit
            obj.gathered = datetime.datetime(2026, 1, 1) + \
                datetime.timedelta(seconds=i)
            obj.save()
            objects.append(obj)
        return source, objects

    def test_quota_is_shared_alternating_between_sources(self):
        # A big backlog (e.g. a first full inventory) and an urgent
        # small update from another source
        big_source, big_objects = self._make_source_with_objects(3)
        small_source, small_objects = self._make_source_with_objects(2)

        # The source with the lowest id is served first
        first_objects, second_objects = sorted(
            [big_objects, small_objects],
            key=lambda objects: objects[0].harvest_source_id)

        sent = queue.send_waiting_objects_to_fetch_queue(max_objects=4)

        # The quota alternates between the sources, keeping each
        # source's own object order
        assert sent == [
            first_objects[0].id, second_objects[0].id,
            first_objects[1].id, second_objects[1].id,
        ]
        assert self._fetch_queue_object_ids() == sent

        # The rest of the backlog was not sent and is not marked
        model.Session.refresh(big_objects[2])
        assert big_objects[2].fetch_queued is None
        assert big_objects[2].state == 'WAITING'

        # The next batch sends what is left of the backlog
        sent = queue.send_waiting_objects_to_fetch_queue(max_objects=4)
        assert sent == [big_objects[2].id]

    def test_batches_continue_rotation_where_they_stopped(self):
        objects = {}
        for _ in range(3):
            source, source_objects = self._make_source_with_objects(2)
            objects[source.id] = source_objects
        s1, s2, s3 = sorted(objects)

        batch1 = queue.send_waiting_objects_to_fetch_queue(max_objects=2)
        assert batch1 == [objects[s1][0].id, objects[s2][0].id]

        # The next batch continues after the last source served, it
        # does not start again from the first one
        batch2 = queue.send_waiting_objects_to_fetch_queue(max_objects=2)
        assert batch2 == [objects[s3][0].id, objects[s1][1].id]

        batch3 = queue.send_waiting_objects_to_fetch_queue(max_objects=2)
        assert batch3 == [objects[s2][1].id, objects[s3][1].id]

        assert queue.send_waiting_objects_to_fetch_queue(max_objects=2) == []
        assert self._fetch_queue_object_ids() == batch1 + batch2 + batch3

    def test_rerun_after_failure_only_sends_what_is_left(self):
        source, objects = self._make_source_with_objects(4)

        # Fail the batch after the first object was dispatched
        failing_publisher = Mock()
        failing_publisher.send.side_effect = [None, Exception('boom')]
        with patch.object(queue, 'get_fetch_publisher',
                          return_value=failing_publisher):
            with pytest.raises(Exception):
                queue.send_waiting_objects_to_fetch_queue(max_objects=4)

        # The dispatched object stays marked; the one that failed to
        # send was released so that it can be sent again
        for obj, dispatched in zip(objects, [True, False, False, False]):
            model.Session.refresh(obj)
            assert (obj.fetch_queued is not None) == dispatched

        # Re-running the same batch recognizes the object that was
        # already dispatched and does not send it again
        sent = queue.send_waiting_objects_to_fetch_queue(max_objects=4)
        assert sent == [obj.id for obj in objects[1:]]
        assert self._fetch_queue_object_ids() == [obj.id for obj in objects[1:]]

    def test_no_quota_limit(self):
        source, objects = self._make_source_with_objects(3)

        sent = queue.send_waiting_objects_to_fetch_queue(max_objects=0)

        assert sent == [obj.id for obj in objects]
        assert self._fetch_queue_object_ids() == sent

    @pytest.mark.ckan_config('ckan.harvest.fetch_queue.max_objects_per_batch', 2)
    def test_quota_defaults_to_config_option(self):
        source, objects = self._make_source_with_objects(3)

        sent = queue.send_waiting_objects_to_fetch_queue()

        assert sent == [obj.id for obj in objects[:2]]


class TestHarvestCorruptRedis(object):

    @patch('ckanext.harvest.queue.log.error')
    def test_redis_corrupt(self, mock_log_error):
        '''
        Test that corrupt Redis doesn't stop harvest process and still processes other jobs.
        '''
        if config.get('ckan.harvest.mq.type') != 'redis':
            pytest.skip()
        redis = queue.get_connection()
        try:
            redis.set('ckanext-harvest:some-random-key-2', 'foobar')

            # make sure queues/exchanges are created first and are empty
            gather_consumer = queue.get_gather_consumer()
            fetch_consumer = queue.get_fetch_consumer()
            gather_consumer.queue_purge(queue=queue.get_gather_queue_name())
            fetch_consumer.queue_purge(queue=queue.get_fetch_queue_name())

            # Create some fake jobs and objects with no harvest_job_id
            gather_publisher = queue.get_gather_publisher()
            gather_publisher.send({'harvest_job_id': str(uuid.uuid4())})
            fetch_publisher = queue.get_fetch_publisher()
            fetch_publisher.send({'harvest_object_id': None})
            h_obj_id = str(uuid.uuid4())
            fetch_publisher.send({'harvest_object_id': h_obj_id})

            # Create some fake objects
            next(gather_consumer.consume(queue.get_gather_queue_name()))
            _, _, body = next(fetch_consumer.consume(queue.get_fetch_queue_name()))

            json_obj = json.loads(body)
            assert json_obj['harvest_object_id'] == h_obj_id

            assert mock_log_error.call_count == 1
            args, _ = mock_log_error.call_args_list[0]
            assert "concatenate" in str(args[1])

        finally:
            redis.delete('ckanext-harvest:some-random-key-2')
