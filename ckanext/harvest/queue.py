import logging
import datetime
import json
import threading
import uuid


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

# Fetch object claims
#
# A harvest object is processed by a single worker at a time. The worker claims
# the object for a limited period and renews the claim from a background thread
# while it runs the fetch/import stages. If the worker dies, the lease expires
# and another worker can take the object over.
DEFAULT_CLAIM_TIMEOUT = 10 * 60  # seconds

# States from which an object will not be processed again. A duplicate delivery
# for an object in one of these states is acknowledged without doing any work.
TERMINAL_OBJECT_STATES = ('COMPLETE', 'ERROR')


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


def get_claim_timeout():
    '''Lease length (in seconds) for a fetch claim on a harvest object.

    Configurable with ``ckan.harvest.fetch_claim_timeout``. A long default is
    used so that a slow but healthy worker is not taken over; it only needs to
    be greater than the interval at which the worker renews its claim.
    '''
    try:
        return int(config.get('ckan.harvest.fetch_claim_timeout',
                              DEFAULT_CLAIM_TIMEOUT))
    except (TypeError, ValueError):
        return DEFAULT_CLAIM_TIMEOUT


def make_claim_token():
    '''Return a fresh token identifying one fetch worker/task.'''
    return uuid.uuid4().hex


def get_live_claim_object_ids(object_ids):
    '''Return the subset of ``object_ids`` that currently hold a live claim.

    Used to avoid re-enqueuing objects that a healthy worker is still busy
    with. A claim is live while ``claim_expires`` is in the future.
    '''
    if not object_ids:
        return set()
    now = datetime.datetime.utcnow()
    rows = model.Session.query(HarvestObject.id).filter(
        HarvestObject.id.in_(list(object_ids)),
        HarvestObject.claim_token.isnot(None),
        HarvestObject.claim_expires.isnot(None),
        HarvestObject.claim_expires > now,
    ).all()
    return {row[0] for row in rows}


class ClaimHeartbeat(object):
    '''Keep a fetch claim alive while its holder is still working.

    A worker renews its lease from a background thread so that long-running
    fetch/import stages are not mistaken for a dead worker. Takeover then only
    happens when the holder has genuinely stopped renewing (i.e. it died),
    which is exactly the distinction between "slow" and "gone" that makes
    lease takeover reliable.
    '''

    def __init__(self, object_id, token, timeout):
        self.object_id = object_id
        self.token = token
        self.timeout = timeout
        # Renew well before expiry so a single slow renewal does not let the
        # lease lapse while the holder is still alive.
        self.interval = max(1, timeout // 3)
        self._stop = threading.Event()
        self._thread = None
        self.lost = False

    def _run(self):
        # This runs on a separate thread. CKAN's session is a scoped (per
        # thread) session, so ``HarvestObject.renew_claim_for`` transparently
        # uses this thread's own session and never the worker's.
        while not self._stop.wait(self.interval):
            try:
                renewed = HarvestObject.renew_claim_for(
                    self.object_id, self.token, self.timeout)
            except Exception:
                log.exception('Error renewing claim on harvest object %s',
                              self.object_id)
                model.Session.remove()
                continue
            model.Session.remove()
            if not renewed:
                # Someone else took the object over; stop trying to hold it.
                self.lost = True
                log.warning('Lost claim on harvest object %s (lease expired)',
                            self.object_id)
                return

    def __enter__(self):
        self._thread = threading.Thread(
            target=self._run, name='harvest-claim-heartbeat', daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        return False


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
            object_id = key.split(':')[-1]
            # Do not re-enqueue an object a live worker is still processing:
            # resubmitting it would create a duplicate delivery. The claim
            # lease is renewed by the worker, so a genuinely stuck worker is
            # handled by the normal takeover once its lease expires.
            if get_live_claim_object_ids([object_id]):
                log.debug('[Fetch queue]: Object {} is still claimed, '
                          'not resubmitting', object_id)
                continue
            log.debug('[Fetch queue]: Re-new harvest object with KEY {} in redis'.format(key))
            redis.rpush(get_fetch_routing_key(),
                        json.dumps({'harvest_object_id': object_id})
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
    Resubmit all WAITING objects on the DB that are not present in Redis
    '''
    if config.get('ckan.harvest.mq.type') != 'redis':
        return
    redis = get_connection()
    publisher = get_fetch_publisher()

    waiting_objects = model.Session.query(HarvestObject.id) \
        .filter_by(state='WAITING') \
        .all()

    objects_in_queue = []
    fetch_routing_key = get_fetch_routing_key()

    objects_in_queue = [json.loads(o)['harvest_object_id']
                        for o in redis.lrange(fetch_routing_key, 0, -1)]

    waiting_ids = [object_id for object_id, in waiting_objects
                   if object_id not in objects_in_queue]
    # Skip objects a live worker is currently processing: re-enqueuing them
    # would only produce duplicate deliveries that the claim would reject.
    claimed_ids = get_live_claim_object_ids(waiting_ids)

    for object_id in waiting_ids:
        if object_id in claimed_ids:
            log.debug('Object {} is still claimed, not resubmitting',
                      object_id)
            continue
        log.debug('Re-sent object {} to the fetch queue'.format(object_id))
        publisher.send({'harvest_object_id': object_id})

    publisher.close()


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

    # Get a publisher for the fetch queue
    publisher = get_fetch_publisher()

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
            publisher.close()
            channel.basic_ack(method.delivery_tag)
            return False

        if len(harvest_object_ids) == 0:
            log.info('No harvest objects to fetch')
            publisher.close()
            channel.basic_ack(method.delivery_tag)
            return False

        log.debug('Received from plugin gather_stage: {0} objects (first: {1} last: {2})'.format(
            len(harvest_object_ids), harvest_object_ids[:1], harvest_object_ids[-1:]))
        for id in harvest_object_ids:
            # Send the id to the fetch queue
            publisher.send({'harvest_object_id': id})
        log.debug('Sent {0} objects to the fetch queue'.format(len(harvest_object_ids)))

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
    publisher.close()
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
    except (KeyError, TypeError, ValueError):
        log.error('No harvest object id received')
        if method.delivery_tag is not None:
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

    # Duplicate deliveries: the same message can arrive more than once (e.g. a
    # resubmit re-enqueued an id that was already queued, or a reconnect
    # redelivered an in-flight message). If the object has already reached a
    # terminal state, acknowledge the message without touching the object.
    if obj.state in TERMINAL_OBJECT_STATES:
        log.info('Harvest object %s already finished (state=%s); '
                 'acknowledging duplicate message', id, obj.state)
        model.Session.remove()
        channel.basic_ack(method.delivery_tag)
        return False

    claim_timeout = get_claim_timeout()
    token = make_claim_token()

    try:
        claimed = obj.claim(token, claim_timeout)
    except sqlalchemy.exc.DatabaseError:
        log.exception('Connection error claiming harvest object %s', id)
        model.Session.remove()
        # Without an ack the message will be retried later.
        return

    if not claimed:
        # Another worker holds a live claim on this object. Leave the message
        # unacknowledged: for Redis this keeps the in-flight marker so a
        # takeover can happen if the holder dies, and for AMQP the broker will
        # redeliver it. Claiming again later will either succeed (the previous
        # holder finished and released, or its lease expired) or be rejected
        # while the holder is still alive.
        log.info('Harvest object %s is already claimed by another worker; '
                 'not processing this delivery', id)
        model.Session.remove()
        return False

    try:
        # Re-read the object now that we hold the claim: its state may have
        # changed since the terminal-state check above (another worker may have
        # completed it between our check and acquiring the now-free claim).
        obj = HarvestObject.get(id)
        if obj is None:
            log.error('Harvest object does not exist: %s' % id)
            channel.basic_ack(method.delivery_tag)
            return False
        if obj.state in TERMINAL_OBJECT_STATES:
            log.info('Harvest object %s already finished (state=%s); '
                     'acknowledging duplicate message', id, obj.state)
            channel.basic_ack(method.delivery_tag)
            return False

        finished = process_claimed_object(obj, token)
    except sqlalchemy.exc.DatabaseError:
        log.exception('Connection Error during fetch of job %s', id)
        # Leave the message unacknowledged so it is retried. The claim is
        # released in the ``finally`` block below.
        finished = False
    finally:
        # Only release a claim we still own; a worker that took the object over
        # after our lease expired owns it now.
        try:
            HarvestObject.release_claim_for(id, token)
        except Exception:
            log.exception('Error releasing claim on harvest object %s', id)
        model.Session.remove()

    if finished:
        channel.basic_ack(method.delivery_tag)
    # Otherwise (the claim was lost mid-processing) leave the message
    # unacknowledged so it can be redelivered / taken over.
    return finished


def process_claimed_object(obj, token):
    '''Run the fetch/import stages for an object we hold the claim on.

    All state writes are fenced on ``token`` so a worker whose lease expired
    can not overwrite the results of the worker that took the object over.

    Returns ``True`` when the object reached a final state (or was deliberately
    left untouched), ``False`` when the claim was lost.
    '''
    # Count the attempt only now that we hold the claim: duplicate deliveries
    # that could not claim never reach this point, so they can not inflate the
    # retry count and push an object into ERROR.
    retry_times = (obj.retry_times or 0) + 1
    if not obj.save_claim_guarded(token, retry_times=retry_times):
        log.warning('Lost claim on harvest object %s before processing', obj.id)
        return False

    if retry_times >= 5:
        if not obj.save_claim_guarded(token, state="ERROR"):
            return False
        log.error('Too many consecutive retries for object {0}'.format(obj.id))
        return True

    # check if job has been set to finished
    job = HarvestJob.get(obj.harvest_job_id)
    if job is not None and job.status == 'Finished':
        if not obj.save_claim_guarded(
                token, state="ERROR", report_status="errored"):
            return False
        log.error('Job {0} was aborted or timed out, object {1} set to error'
                  .format(job.id, obj.id))
        return True

    # Send the harvest object to the plugins that implement
    # the Harvester interface, only if the source type
    # matches. Keep the lease alive from a background thread so a long but
    # healthy fetch/import is not mistaken for a dead worker.
    with ClaimHeartbeat(obj.id, token, get_claim_timeout()):
        for harvester in PluginImplementations(IHarvester):
            if harvester.info()['name'] == obj.source.type:
                if not fetch_and_import_stages(harvester, obj, token=token):
                    return False
    return True


def fetch_and_import_stages(harvester, obj, token=None):
    '''Run ``fetch_stage`` and, if successful, ``import_stage`` for ``obj``.

    When ``token`` is given the object is being processed under a fetch claim.
    The claim is renewed at each stage boundary and every write is fenced on
    the token, so a worker that lost its lease (because it took too long and
    the object was taken over) discards its result instead of overwriting the
    new holder's. ``token=None`` keeps the historical behaviour for callers
    that run the stages directly (tests, ``harvester run-test``).
    '''
    claim_timeout = get_claim_timeout()

    def renew():
        if token is None:
            return True
        return obj.renew_claim(token, claim_timeout)

    def save(**values):
        if token is None:
            for key, value in values.items():
                setattr(obj, key, value)
            obj.save()
            return True
        # In fenced mode the write is a bare UPDATE (compare-and-set on the
        # claim token), not an ORM flush, so the guard - not the ORM - decides
        # whether the row is written. ``obj`` is kept in sync only after a
        # successful write, and then as committed values so nothing is left
        # pending for a later commit to flush unguarded.
        return obj.save_claim_guarded(token, **values)

    if not renew():
        return False
    if not save(fetch_started=datetime.datetime.utcnow(), state="FETCH"):
        return False

    success_fetch = harvester.fetch_stage(obj)

    if not renew():
        return False
    if not save(fetch_finished=datetime.datetime.utcnow()):
        return False

    if success_fetch is True:
        # If no errors where found, call the import method
        if not renew():
            return False
        if not save(import_started=datetime.datetime.utcnow(), state="IMPORT"):
            return False
        success_import = harvester.import_stage(obj)
        if not renew():
            return False
        if not save(import_finished=datetime.datetime.utcnow()):
            return False
        if success_import:
            state = "COMPLETE"
            report_status = "not modified" if success_import == 'unchanged' else None
        else:
            state = "ERROR"
            report_status = "errored"
    elif success_fetch == 'unchanged':
        state = "COMPLETE"
        report_status = "not modified"
    else:
        state = "ERROR"
        report_status = "errored"

    if report_status is None:
        if obj.current is False:
            report_status = "deleted"
        elif len(
            model.Session.query(HarvestObject)
                .filter_by(package_id=obj.package_id)
                .limit(2)
                .all()
        ) == 2:
            report_status = "updated"
        else:
            report_status = "added"

    return save(state=state, report_status=report_status)


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
