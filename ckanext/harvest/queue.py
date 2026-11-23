import logging
import datetime
import json

from collections import deque

import redis
try:
    import pika
except ImportError:
    pika = None
import sqlalchemy

from ckan.lib.base import config
from ckan.plugins import PluginImplementations
from ckan import model

from ckanext.harvest.model import HarvestJob, HarvestObject, HarvestGatherError
from ckanext.harvest.interfaces import IHarvester

log = logging.getLogger(__name__)
assert not log.disabled

__all__ = ['get_gather_publisher', 'get_gather_consumer',
           'get_fetch_publisher', 'get_fetch_consumer',
           'get_harvester']

PORT = 5672
USERID = 'guest'
PASSWORD = 'guest'
HOSTNAME = 'localhost'
VIRTUAL_HOST = '/'
MQ_TYPE = 'redis'
REDIS_PORT = 6379
REDIS_DB = 0

# settings for AMQP
EXCHANGE_TYPE = 'direct'
EXCHANGE_NAME = 'ckan.harvest'

# Default number of waiting harvest objects sent to the fetch queue in one
# batch by send_waiting_objects_to_fetch_queue. Can be overridden with the
# `ckan.harvest.fetch_queue.max_objects_per_batch` config option.
MAX_OBJECTS_PER_FETCH_BATCH = 100


def get_connection():
    backend = config.get('ckan.harvest.mq.type', MQ_TYPE)
    if backend in ('amqp', 'ampq'):  # "ampq" is for compat with old typo
        return get_connection_amqp()
    if backend == 'redis':
        return get_connection_redis()
    raise Exception('not a valid queue type %s' % backend)


def get_connection_amqp():

    if not pika:
        raise ValueError("The pika library is needed to use RabbitMQ as the backend"
                         "Install it with `pip install ckanext-harvest[amqp]`")
    try:
        port = int(config.get('ckan.harvest.mq.port', PORT))
    except ValueError:
        port = PORT
    userid = config.get('ckan.harvest.mq.user_id', USERID)
    password = config.get('ckan.harvest.mq.password', PASSWORD)
    hostname = config.get('ckan.harvest.mq.hostname', HOSTNAME)
    virtual_host = config.get('ckan.harvest.mq.virtual_host', VIRTUAL_HOST)

    credentials = pika.PlainCredentials(userid, password)
    parameters = pika.ConnectionParameters(host=hostname,
                                           port=port,
                                           virtual_host=virtual_host,
                                           credentials=credentials,
                                           frame_max=10000)
    log.debug("pika connection using %s" % parameters)

    return pika.BlockingConnection(parameters)


def get_connection_redis():
    # redis-py >= 8 defaults ``socket_timeout`` to 5 seconds, whereas older
    # versions blocked forever. The gather/fetch consumers call ``blpop`` with
    # no timeout and rely on the socket blocking until a message arrives, so
    # force blocking behaviour explicitly with ``socket_timeout=None``.
    if not config.get('ckan.harvest.mq.hostname') and config.get('ckan.redis.url'):
        return redis.Redis.from_url(
            config['ckan.redis.url'],
            decode_responses=True,
            encoding='utf-8',
            socket_timeout=None,
        )
    else:
        return redis.Redis(
            host=config.get('ckan.harvest.mq.hostname', HOSTNAME),
            port=int(config.get('ckan.harvest.mq.port', REDIS_PORT)),
            password=config.get('ckan.harvest.mq.password', None),
            db=int(config.get('ckan.harvest.mq.redis_db', REDIS_DB)),
            decode_responses=True,
            encoding='utf-8',
            socket_timeout=None,
        )


def get_gather_queue_name():
    return 'ckan.harvest.{0}.gather'.format(config.get('ckan.site_id',
                                                       'default'))


def get_fetch_queue_name():
    return 'ckan.harvest.{0}.fetch'.format(config.get('ckan.site_id',
                                                      'default'))


def get_gather_routing_key():
    return 'ckanext-harvest:{0}:harvest_job_id'.format(
        config.get('ckan.site_id', 'default'))


def get_fetch_routing_key():
    return 'ckanext-harvest:{0}:harvest_object_id'.format(
        config.get('ckan.site_id', 'default'))


def purge_queues():
    backend = config.get('ckan.harvest.mq.type', MQ_TYPE)
    connection = get_connection()
    if backend in ('amqp', 'ampq'):
        channel = connection.channel()
        channel.queue_purge(queue=get_gather_queue_name())
        log.info('AMQP queue purged: %s', get_gather_queue_name())
        channel.queue_purge(queue=get_fetch_queue_name())
        log.info('AMQP queue purged: %s', get_fetch_queue_name())
    elif backend == 'redis':
        get_gather_consumer().queue_purge()
        log.info('Redis gather queue purged')
        get_fetch_consumer().queue_purge()
        log.info('Redis fetch queue purged')


def resubmit_jobs():
    '''
    Examines the fetch and gather queues for items that are suspiciously old.
    These are removed from the queues and placed back on them afresh, to ensure
    the fetch & gather consumers are triggered to process it.
    '''
    if config.get('ckan.harvest.mq.type') != 'redis':
        return
    redis = get_connection()

    # fetch queue
    harvest_object_pending = redis.keys(get_fetch_routing_key() + ':*')
    for key in harvest_object_pending:
        redis_value = redis.get(key)
        if redis_value is None:
            log.info('Fetch Queue: Redis cannot get value for key {}'.format(key))
            continue
        date_of_key = datetime.datetime.strptime(
            redis_value, "%Y-%m-%d %H:%M:%S.%f")
        log.debug('[Fetch queue]: Check key {} with value {}'.format(key, date_of_key))
        # 3 minutes for fetch and import max
        if (datetime.datetime.now() - date_of_key).seconds > 180:
            log.debug('[Fetch queue]: Re-new harvest object with KEY {} in redis'.format(key))
            redis.rpush(get_fetch_routing_key(),
                        json.dumps({'harvest_object_id': key.split(':')[-1]})
                        )
            redis.delete(key)

    # gather queue
    harvest_jobs_pending = redis.keys(get_gather_routing_key() + ':*')
    for key in harvest_jobs_pending:
        redis_value = redis.get(key)
        if redis_value is None:
            log.info('Gather Queue: Redis cannot get value for key {}'.format(key))
            continue
        date_of_key = datetime.datetime.strptime(
            redis_value, "%Y-%m-%d %H:%M:%S.%f")
        log.debug('[Gather queue]: Check key {} with value {}'.format(key, date_of_key))
        # 3 hours for a gather
        if (datetime.datetime.now() - date_of_key).seconds > 7200:
            log.debug('[Gather queue]: Re-new harvest job with KEY {} in redis'.format(key))
            redis.rpush(get_gather_routing_key(),
                        json.dumps({'harvest_job_id': key.split(':')[-1]})
                        )
            redis.delete(key)


def resubmit_objects():
    '''
    Resubmit all WAITING objects on the DB that were already dispatched to
    the fetch queue (``fetch_queued`` is set) but are not present in Redis.

    Objects that have not been dispatched yet are left alone: they will be
    sent to the fetch queue by ``send_waiting_objects_to_fetch_queue``,
    respecting its per-batch quota.
    '''
    if config.get('ckan.harvest.mq.type') != 'redis':
        return
    redis = get_connection()
    publisher = get_fetch_publisher()

    dispatched_objects = model.Session.query(HarvestObject.id) \
        .filter(HarvestObject.state == 'WAITING') \
        .filter(HarvestObject.fetch_queued.isnot(None)) \
        .all()

    objects_in_queue = []
    fetch_routing_key = get_fetch_routing_key()

    objects_in_queue = [json.loads(o)['harvest_object_id']
                        for o in redis.lrange(fetch_routing_key, 0, -1)]

    for object_id, in dispatched_objects:
        if object_id not in objects_in_queue:
            log.debug('Re-sent object {} to the fetch queue'.format(object_id))
            publisher.send({'harvest_object_id': object_id})

    publisher.close()


def _waiting_objects_filters():
    '''Filters selecting harvest objects still waiting to be dispatched'''
    return [
        HarvestObject.state == 'WAITING',
        HarvestObject.fetch_queued.is_(None),
    ]


def send_waiting_objects_to_fetch_queue(max_objects=None):
    '''
    Sends up to ``max_objects`` WAITING harvest objects to the fetch queue.

    To stop a source with a large backlog (e.g. the first full inventory
    of a big source) from holding up urgent objects from other sources,
    the available quota is shared out alternating between the sources
    that have waiting objects, one object per source at a time, keeping
    each source's own object order. The next batch continues the
    rotation where the previous one stopped, so every source keeps
    getting a fair share of the fetch queue.

    Each object is marked (``fetch_queued``) as it is dispatched, so if
    a batch fails part way through and is re-run, the objects that were
    already dispatched are recognized and not sent again.

    :param max_objects: the quota for this batch. Defaults to the
        ``ckan.harvest.fetch_queue.max_objects_per_batch`` config
        option. A value of 0 or less means no limit.
    :returns: the ids of the harvest objects sent to the fetch queue
    '''
    if max_objects is None:
        try:
            max_objects = int(config.get(
                'ckan.harvest.fetch_queue.max_objects_per_batch',
                MAX_OBJECTS_PER_FETCH_BATCH))
        except ValueError:
            max_objects = MAX_OBJECTS_PER_FETCH_BATCH
    unlimited = max_objects <= 0

    waiting_filters = _waiting_objects_filters()

    # Sources with objects waiting to be dispatched, in a stable order
    source_ids = [
        row[0] for row in
        model.Session.query(HarvestObject.harvest_source_id)
        .filter(*waiting_filters)
        .distinct()
        .order_by(HarvestObject.harvest_source_id)
        .all()
    ]
    if not source_ids:
        return []

    # Continue the rotation where the previous batch stopped: the last
    # source served is the one the most recently dispatched object
    # belongs to
    last_dispatched = (
        model.Session.query(HarvestObject.harvest_source_id)
        .filter(HarvestObject.fetch_queued.isnot(None))
        .order_by(HarvestObject.fetch_queued.desc(), HarvestObject.id.desc())
        .first()
    )
    if last_dispatched:
        last_source_id = last_dispatched[0]
        source_ids = (
            [s for s in source_ids if s > last_source_id] +
            [s for s in source_ids if s <= last_source_id]
        )

    # Candidates for each source, in the source's own (gather) order
    pending_by_source = {}
    for source_id in source_ids:
        query = (
            model.Session.query(HarvestObject.id)
            .filter(HarvestObject.harvest_source_id == source_id)
            .filter(*waiting_filters)
            .order_by(HarvestObject.gathered, HarvestObject.id)
        )
        if not unlimited:
            query = query.limit(max_objects)
        pending_by_source[source_id] = deque(row[0] for row in query.all())

    # Alternate between the sources, one object at a time, until the
    # quota is used up or there is nothing left to send
    selected = []
    while unlimited or len(selected) < max_objects:
        found = False
        for source_id in source_ids:
            pending = pending_by_source[source_id]
            if pending:
                selected.append(pending.popleft())
                found = True
                if not unlimited and len(selected) >= max_objects:
                    break
        if not found:
            break

    if not selected:
        return []

    publisher = get_fetch_publisher()
    sent = []
    try:
        for object_id in selected:
            # Claim the object before sending it, so that objects that
            # were already dispatched (e.g. when the same batch is
            # re-run after a failure, or a concurrent dispatcher claimed
            # it first) are recognized and skipped
            claimed = (
                model.Session.query(HarvestObject)
                .filter(HarvestObject.id == object_id)
                .filter(*waiting_filters)
                .update(
                    {'fetch_queued': datetime.datetime.utcnow()},
                    synchronize_session=False)
            )
            model.Session.commit()
            if not claimed:
                continue
            try:
                publisher.send({'harvest_object_id': object_id})
            except Exception:
                # Could not be dispatched: release the claim so that a
                # later batch tries to send it again
                (
                    model.Session.query(HarvestObject)
                    .filter(HarvestObject.id == object_id)
                    .update({'fetch_queued': None},
                            synchronize_session=False)
                )
                model.Session.commit()
                raise
            sent.append(object_id)
    finally:
        publisher.close()

    log.debug('Sent {0} objects to the fetch queue'.format(len(sent)))
    return sent


class Publisher(object):
    def __init__(self, connection, channel, exchange, routing_key):
        self.connection = connection
        self.channel = channel
        self.exchange = exchange
        self.routing_key = routing_key

    def send(self, body, **kw):
        return self.channel.basic_publish(
            self.exchange,
            self.routing_key,
            json.dumps(body),
            properties=pika.BasicProperties(
                delivery_mode=2,  # make message persistent
            ),
            **kw)

    def close(self):
        self.connection.close()


class RedisPublisher(object):
    def __init__(self, redis, routing_key):
        self.redis = redis  # not used
        self.routing_key = routing_key

    def send(self, body, **kw):
        value = json.dumps(body)
        # remove if already there
        if self.routing_key == get_gather_routing_key():
            # it appears that both types of call are possible within the redis library depending on which version used
            # for now support both versions
            # https://github.com/andymccurdy/redis-py#client-classes-redis-and-strictredis
            try:
                self.redis.lrem(self.routing_key, 0, value)
            except redis.ResponseError as e:
                if 'value is not an integer' in e.message:
                    self.redis.lrem(self.routing_key, value, 0)
                else:
                    raise
        self.redis.rpush(self.routing_key, value)

    def close(self):
        return


def get_publisher(routing_key):
    connection = get_connection()
    backend = config.get('ckan.harvest.mq.type', MQ_TYPE)
    if backend in ('amqp', 'ampq'):
        channel = connection.channel()
        channel.exchange_declare(exchange=EXCHANGE_NAME, durable=True)
        return Publisher(connection,
                         channel,
                         EXCHANGE_NAME,
                         routing_key=routing_key)
    if backend == 'redis':
        return RedisPublisher(connection, routing_key)


class FakeMethod(object):
    ''' This is to act like the method returned by AMQP'''
    def __init__(self, message):
        self.delivery_tag = message


class RedisConsumer(object):
    def __init__(self, redis, routing_key):
        self.redis = redis
        # Routing keys are constructed with {site-id}:{message-key}, eg:
        # default:harvest_job_id or default:harvest_object_id
        self.routing_key = routing_key
        # Message keys are harvest_job_id for the gather consumer and
        # harvest_object_id for the fetch consumer
        self.message_key = routing_key.split(':')[-1]

    def consume(self, queue):
        while True:
            key, body = self.redis.blpop(self.routing_key)
            try:
                self.redis.set(self.persistance_key(body), str(datetime.datetime.now()))
            except Exception as e:
                log.error("Redis Exception: %s", e)
                continue

            yield (FakeMethod(body), self, body)

    def persistance_key(self, message):
        # If you change this, make sure to update the script in `queue_purge`
        message = json.loads(message)
        return self.routing_key + ':' + message[self.message_key]

    def basic_ack(self, message):
        self.redis.delete(self.persistance_key(message))

    def queue_purge(self, queue=None):
        '''
        Purge the consumer's queue.

        The ``queue`` parameter exists only for compatibility and is
        ignored.
        '''
        # Use a script to make the operation atomic
        lua_code = b'''
            local routing_key = KEYS[1]
            local message_key = ARGV[1]
            local count = 0
            while true do
                local s = redis.call("lpop", routing_key)
                if s == false then
                    break
                end
                local value = cjson.decode(s)
                local id = value[message_key]
                local persistance_key = routing_key .. ":" .. id
                redis.call("del", persistance_key)
                count = count + 1
            end
            return count
        '''
        script = self.redis.register_script(lua_code)
        return script(keys=[self.routing_key], args=[self.message_key])

    def basic_get(self, queue):
        body = self.redis.lpop(self.routing_key)
        return (FakeMethod(body), self, body)


def get_consumer(queue_name, routing_key):

    connection = get_connection()
    backend = config.get('ckan.harvest.mq.type', MQ_TYPE)

    if backend in ('amqp', 'ampq'):
        channel = connection.channel()
        channel.exchange_declare(exchange=EXCHANGE_NAME, durable=True)
        channel.queue_declare(queue=queue_name, durable=True)
        channel.queue_bind(queue=queue_name, exchange=EXCHANGE_NAME, routing_key=routing_key)
        return channel
    if backend == 'redis':
        return RedisConsumer(connection, routing_key)


def gather_callback(channel, method, header, body):

    try:
        id = json.loads(body)['harvest_job_id']
        log.debug('Received harvest job id: %s' % id)
    except KeyError:
        log.error('No harvest job id received')
        channel.basic_ack(method.delivery_tag)
        return False

    try:
        job = HarvestJob.get(id)
    except sqlalchemy.exc.DatabaseError:
        # Occasionally we see: sqlalchemy.exc.OperationalError
        # "SSL connection has been closed unexpectedly"
        # or DatabaseError "connection timed out"
        log.exception('Connection Error during gather of job %s', id)
        # By not sending the ack, it will be retried later.
        # Try to clear the issue with a remove.
        model.Session.remove()
        return
    if not job:
        log.error('Harvest job does not exist: %s' % id)
        channel.basic_ack(method.delivery_tag)
        return False

    # Send the harvest job to the plugins that implement
    # the Harvester interface, only if the source type
    # matches
    harvester = get_harvester(job.source.type)
    if harvester:
        try:
            harvest_object_ids = gather_stage(harvester, job)
        except (Exception, KeyboardInterrupt):
            channel.basic_ack(method.delivery_tag)
            raise

        if not isinstance(harvest_object_ids, list):
            log.error('Gather stage failed')
            channel.basic_ack(method.delivery_tag)
            return False

        if len(harvest_object_ids) == 0:
            log.info('No harvest objects to fetch')
            channel.basic_ack(method.delivery_tag)
            return False

        log.debug('Received from plugin gather_stage: {0} objects (first: {1} last: {2})'.format(
            len(harvest_object_ids), harvest_object_ids[:1], harvest_object_ids[-1:]))
        # Send a first batch of the waiting objects to the fetch queue.
        # Objects are sent in limited batches, alternating between
        # sources, so that a source with many objects (e.g. a first full
        # inventory) does not hold up the remaining sources. Further
        # batches are sent on the next `harvest_jobs_run` calls.
        send_waiting_objects_to_fetch_queue()

    else:
        # This can occur if you:
        # * remove a harvester and it still has sources that are then refreshed
        # * add a new harvester and restart CKAN but not the gather queue.
        msg = 'System error - No harvester could be found for source type %s' % job.source.type
        err = HarvestGatherError(message=msg, job=job)
        err.save()
        log.error(msg)
        job.status = u'Finished'
        job.save()
        log.info('Marking job as finished due to error: %s %s',
                 job.source.url, job.id)

    model.Session.remove()
    channel.basic_ack(method.delivery_tag)


def get_harvester(harvest_source_type):
    for harvester in PluginImplementations(IHarvester):
        if harvester.info()['name'] == harvest_source_type:
            return harvester


def gather_stage(harvester, job):
    '''Calls the harvester's gather_stage, returning harvest object ids, with
    some error handling.

    This is split off from gather_callback so that tests can call it without
    dealing with queue stuff.
    '''
    job.gather_started = datetime.datetime.utcnow()

    try:
        harvest_object_ids = harvester.gather_stage(job)
    except (Exception, KeyboardInterrupt):
        harvest_objects = model.Session.query(HarvestObject).filter_by(
            harvest_job_id=job.id
        )
        for harvest_object in harvest_objects:
            model.Session.delete(harvest_object)
        model.Session.commit()
        raise
    finally:
        job.gather_finished = datetime.datetime.utcnow()
        job.save()
    return harvest_object_ids


def fetch_callback(channel, method, header, body):
    try:
        id = json.loads(body)['harvest_object_id']
        log.info('Received harvest object id: %s' % id)
    except KeyError:
        log.error('No harvest object id received')
        channel.basic_ack(method.delivery_tag)
        return False

    try:
        obj = HarvestObject.get(id)
    except sqlalchemy.exc.DatabaseError:
        # Occasionally we see: sqlalchemy.exc.OperationalError
        # "SSL connection has been closed unexpectedly"
        # or DatabaseError "connection timed out"
        log.exception('Connection Error during fetch of job %s', id)
        # By not sending the ack, it will be retried later.
        # Try to clear the issue with a remove.
        model.Session.remove()
        return
    if not obj:
        log.error('Harvest object does not exist: %s' % id)
        channel.basic_ack(method.delivery_tag)
        return False

    obj.retry_times += 1
    obj.save()

    if obj.retry_times >= 5:
        obj.state = "ERROR"
        obj.save()
        log.error('Too many consecutive retries for object {0}'.format(obj.id))
        channel.basic_ack(method.delivery_tag)
        return False

    # check if job has been set to finished
    job = HarvestJob.get(obj.harvest_job_id)
    if job.status == 'Finished':
        obj.state = "ERROR"
        obj.report_status = "errored"
        obj.save()
        log.error('Job {0} was aborted or timed out, object {1} set to error'.format(job.id, obj.id))
        channel.basic_ack(method.delivery_tag)
        return False

    # Send the harvest object to the plugins that implement
    # the Harvester interface, only if the source type
    # matches
    for harvester in PluginImplementations(IHarvester):
        if harvester.info()['name'] == obj.source.type:
            fetch_and_import_stages(harvester, obj)

    model.Session.remove()
    channel.basic_ack(method.delivery_tag)


def fetch_and_import_stages(harvester, obj):
    obj.fetch_started = datetime.datetime.utcnow()
    obj.state = "FETCH"
    obj.save()
    success_fetch = harvester.fetch_stage(obj)
    obj.fetch_finished = datetime.datetime.utcnow()
    obj.save()
    if success_fetch is True:
        # If no errors where found, call the import method
        obj.import_started = datetime.datetime.utcnow()
        obj.state = "IMPORT"
        obj.save()
        success_import = harvester.import_stage(obj)
        obj.import_finished = datetime.datetime.utcnow()
        if success_import:
            obj.state = "COMPLETE"
            if success_import == 'unchanged':
                obj.report_status = 'not modified'
                obj.save()
                return
        else:
            obj.state = "ERROR"
        obj.save()
    elif success_fetch == 'unchanged':
        obj.state = 'COMPLETE'
        obj.report_status = 'not modified'
        obj.save()
        return
    else:
        obj.state = "ERROR"
        obj.save()
    if obj.state == 'ERROR':
        obj.report_status = 'errored'
    elif obj.current is False:
        obj.report_status = 'deleted'
    elif len(
        model.Session.query(HarvestObject)
            .filter_by(package_id=obj.package_id)
            .limit(2)
            .all()
    ) == 2:
        obj.report_status = 'updated'
    else:
        obj.report_status = 'added'
    obj.save()


def get_gather_consumer():
    gather_routing_key = get_gather_routing_key()
    consumer = get_consumer(get_gather_queue_name(), gather_routing_key)
    log.debug('Gather queue consumer registered')
    return consumer


def get_fetch_consumer():
    fetch_routing_key = get_fetch_routing_key()
    consumer = get_consumer(get_fetch_queue_name(), fetch_routing_key)
    log.debug('Fetch queue consumer registered')
    return consumer


def get_gather_publisher():
    gather_routing_key = get_gather_routing_key()
    return get_publisher(gather_routing_key)


def get_fetch_publisher():
    fetch_routing_key = get_fetch_routing_key()
    return get_publisher(fetch_routing_key)
