import logging
import datetime

from sqlalchemy import event
from sqlalchemy import Column
from sqlalchemy import ForeignKey
from sqlalchemy import types
from sqlalchemy import Index
from sqlalchemy import or_
from sqlalchemy.orm import backref, relationship
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.exc import InvalidRequestError

from ckan.model.meta import Session
from ckan.model.types import make_uuid
from ckan.model.domain_object import DomainObject
from ckan.model.package import Package

try:
    from ckan.plugins.toolkit import BaseModel
except ImportError:
    # CKAN <= 2.9
    from ckan.model.meta import metadata
    from sqlalchemy.ext.declarative import declarative_base

    BaseModel = declarative_base(metadata=metadata)


UPDATE_FREQUENCIES = ["MANUAL", "MONTHLY", "WEEKLY", "BIWEEKLY", "DAILY", "ALWAYS"]

log = logging.getLogger(__name__)


class HarvestError(Exception):
    pass


class HarvestDomainObject(DomainObject):
    """Convenience methods for searching objects"""

    key_attr = "id"

    @classmethod
    def get(cls, key, default=None, attr=None):
        """Finds a single entity in the register."""
        if attr is None:
            attr = cls.key_attr
        kwds = {attr: key}
        o = cls.filter(**kwds).first()
        if o:
            return o
        else:
            return default

    @classmethod
    def filter(cls, **kwds):
        query = Session.query(cls).autoflush(False)
        return query.filter_by(**kwds)


class HarvestSource(BaseModel, HarvestDomainObject):
    """A Harvest Source is essentially a URL plus some other metadata.
    It must have a type (e.g. CSW) and can have a status of "active"
    or "inactive". The harvesting processes are not fired on inactive
    sources.
    """

    __tablename__ = "harvest_source"

    id = Column(types.UnicodeText, primary_key=True, default=make_uuid)
    url = Column(types.UnicodeText, nullable=False)
    title = Column(types.UnicodeText, default="")
    description = Column(types.UnicodeText, default="")
    config = Column(types.UnicodeText, default="")
    created = Column(types.DateTime, default=datetime.datetime.utcnow)
    type = Column(types.UnicodeText, nullable=False)
    active = Column(types.Boolean, default=True)
    user_id = Column(types.UnicodeText, default="")
    publisher_id = Column(types.UnicodeText, default="")
    frequency = Column(types.UnicodeText, default="MANUAL")
    next_run = Column(types.DateTime)
    jobs = relationship(
        "HarvestJob",
        lazy="select",
        back_populates="source",
        order_by=lambda: HarvestJob.created,
    )

    def __repr__(self):
        return "<HarvestSource id=%s title=%s url=%s active=%r>" % (
            self.id,
            self.title,
            self.url,
            self.active,
        )

    def __str__(self):
        return self.__repr__().encode("ascii", "ignore")

    def get_jobs(self, status=None):
        """get the running jobs for this source"""

        query = Session.query(HarvestJob).filter(HarvestJob.source_id == self.id)

        if status is not None:
            query = query.filter(HarvestJob.status == status)

        return query.all()


class HarvestJob(BaseModel, HarvestDomainObject):
    """A Harvesting Job is performed in two phases. In first place, the
    **gather** stage collects all the Ids and URLs that need to be fetched
    from the harvest source. Errors occurring in this phase
    (``HarvestGatherError``) are stored in the ``harvest_gather_error``
    table. During the next phase, the **fetch** stage retrieves the
    ``HarvestedObjects`` and, if necessary, the **import** stage stores
    them on the database. Errors occurring in this second stage
    (``HarvestObjectError``) are stored in the ``harvest_object_error``
    table.
    """

    __tablename__ = "harvest_job"

    id = Column(types.UnicodeText, primary_key=True, default=make_uuid)
    created = Column(types.DateTime, default=datetime.datetime.utcnow)
    gather_started = Column(types.DateTime)
    gather_finished = Column(types.DateTime)
    finished = Column(types.DateTime)
    source_id = Column(types.UnicodeText, ForeignKey("harvest_source.id"))
    # status: New, Running, Finished
    status = Column(types.UnicodeText, default="New", nullable=False)
    source = relationship(
        "HarvestSource",
        lazy="select",
        back_populates="jobs",
    )

    def get_last_finished_object(self):
        """Determine the last finished object in this job
        Helpful to know if a job is running or not and
          to avoid timeouts when the source is running
        """

        query = (
            Session.query(HarvestObject)
            .filter(HarvestObject.harvest_job_id == self.id)
            .filter(HarvestObject.state == "COMPLETE")
            .filter(HarvestObject.import_finished.isnot(None))
            .order_by(HarvestObject.import_finished.desc())
            .first()
        )

        return query

    def get_last_gathered_object(self):
        """Determine the last gathered object in this job
        Helpful to know if a job is running or not and
          to avoid timeouts when the source is running
        """

        query = (
            Session.query(HarvestObject)
            .filter(HarvestObject.harvest_job_id == self.id)
            .order_by(HarvestObject.gathered.desc())
            .first()
        )

        return query

    def get_last_action_time(self):
        last_object = self.get_last_finished_object()
        if last_object is not None:
            return last_object.import_finished

        if self.gather_finished is not None:
            return self.gather_finished

        last_gathered_object = self.get_last_gathered_object()
        if last_gathered_object is not None:
            return last_gathered_object.gathered

        return self.created

    def get_gather_errors(self):
        query = (
            Session.query(HarvestGatherError)
            .filter(HarvestGatherError.harvest_job_id == self.id)
            .order_by(HarvestGatherError.created.desc())
        )

        return query.all()


class HarvestObject(BaseModel, HarvestDomainObject):
    """A Harvest Object is created every time an element is fetched from a
    harvest source. Its contents can be processed and imported to ckan
    packages, RDF graphs, etc.

    """

    __tablename__ = "harvest_object"

    id = Column(types.UnicodeText, primary_key=True, default=make_uuid)
    # The guid is the 'identity' of the dataset, according to the source.
    # So if you reharvest it, then the harvester knows which dataset to
    # update because of this identity. The identity needs to be unique
    # within this CKAN.
    guid = Column(types.UnicodeText, default="")
    # When you harvest a dataset multiple times, only the latest
    # successfully imported harvest_object should be flagged 'current'.
    # The import_stage usually reads and writes it.
    current = Column(types.Boolean, default=False)
    gathered = Column(types.DateTime, default=datetime.datetime.utcnow)
    fetch_started = Column(types.DateTime)
    content = Column(types.UnicodeText, nullable=True)
    fetch_finished = Column(types.DateTime)
    import_started = Column(types.DateTime)
    import_finished = Column(types.DateTime)
    # state: WAITING, FETCH, IMPORT, COMPLETE, ERROR
    state = Column(types.UnicodeText, default="WAITING")
    metadata_modified_date = Column(types.DateTime)
    retry_times = Column(types.Integer, default=0)
    harvest_job_id = Column(types.UnicodeText, ForeignKey("harvest_job.id"))
    harvest_source_id = Column(types.UnicodeText, ForeignKey("harvest_source.id"))
    package_id = Column(
        types.UnicodeText,
        ForeignKey("package.id", deferrable=True),
        nullable=True,
    )
    # report_status: 'added', 'updated', 'not modified', 'deleted', 'errored'
    report_status = Column(types.UnicodeText, nullable=True)
    # Fetch claims make sure a single worker processes a given object even if
    # the same message is delivered more than once (e.g. redelivery after a
    # reconnect, or two workers popping the same message). ``claim_token``
    # identifies the worker holding the object, ``claim_expires`` is the
    # instant after which another worker may take the object over.
    claim_token = Column(types.UnicodeText, nullable=True)
    claim_expires = Column(types.DateTime, nullable=True)
    harvest_job_id_idx = Index("harvest_job_id")
    harvest_source_id_idx = Index("harvest_source_id")
    package_id_idx = Index("package_id")
    guid_idx = Index("guid")
    package = relationship(
        Package,
        lazy="select",
        backref="harvest_objects",
    )
    job = relationship(
        HarvestJob,
        lazy="select",
        backref="objects",
    )
    source = relationship(
        HarvestSource,
        lazy="select",
        backref="objects",
    )

    # -- Fetch claims -----------------------------------------------------
    #
    # The fetch queue is at-least-once: a message can be delivered twice (a
    # consumer reconnecting may get the same message again, or a resubmit can
    # enqueue an id that is already queued) and two workers can end up with
    # the same object. To make sure a single worker processes an object at a
    # time, a worker claims it before running the fetch/import stages.
    #
    # The claim is a lease: ``claim_token`` identifies the worker and
    # ``claim_expires`` is the instant after which the object may be taken
    # over by another worker. The holder renews the lease while it works, so a
    # dead consumer's objects are recoverable once the lease expires.
    #
    # Result fields are written with a *fenced* update that only succeeds while
    # ``claim_token`` still matches, so a slow worker whose lease expired (and
    # whose object was taken over) cannot overwrite the new holder's result.

    @classmethod
    def claim_for(cls, object_id, token, timeout):
        """Atomically try to acquire (or renew) the claim on ``object_id``.

        The single ``UPDATE ... WHERE`` statement is the atomic step: only one
        of any number of concurrent callers can move an unclaimed or expired
        object to its own token. On PostgreSQL the concurrent updater blocks
        until the winner commits, then re-evaluates the predicate against the
        committed row and matches nothing.

        :param object_id: id of the harvest object to claim.
        :param token: unique string identifying the claiming worker/task.
        :param timeout: lease length in seconds.
        :returns: ``True`` if the claim is now held by ``token``.
        """
        now = datetime.datetime.utcnow()
        expires = now + datetime.timedelta(seconds=timeout)
        table = cls.__table__
        stmt = (
            table.update()
            .where(table.c.id == object_id)
            .where(
                or_(
                    table.c.claim_token.is_(None),
                    table.c.claim_expires.is_(None),
                    table.c.claim_expires <= now,
                    table.c.claim_token == token,
                )
            )
            .values(claim_token=token, claim_expires=expires)
            .execution_options(synchronize_session=False)
        )
        return cls._run_fenced(stmt)

    @classmethod
    def renew_claim_for(cls, object_id, token, timeout):
        """Extend the lease, but only if ``token`` still holds it.

        Unlike :meth:`claim_for`, an expired claim held by *another* token is
        not taken over here. This doubles as an ownership check made
        immediately before a worker writes anything.
        """
        expires = datetime.datetime.utcnow() + datetime.timedelta(
            seconds=timeout
        )
        table = cls.__table__
        stmt = (
            table.update()
            .where(table.c.id == object_id)
            .where(table.c.claim_token == token)
            .values(claim_expires=expires)
            .execution_options(synchronize_session=False)
        )
        return cls._run_fenced(stmt)

    @classmethod
    def release_claim_for(cls, object_id, token):
        """Drop the claim, but only if it is still held by ``token``."""
        table = cls.__table__
        stmt = (
            table.update()
            .where(table.c.id == object_id)
            .where(table.c.claim_token == token)
            .values(claim_token=None, claim_expires=None)
            .execution_options(synchronize_session=False)
        )
        return cls._run_fenced(stmt)

    @classmethod
    def save_claim_guarded_for(cls, object_id, token, **values):
        """Write ``values`` only while ``token`` still holds the claim.

        This is a compare-and-set on the claim token. When the lease was lost
        (the object was taken over by another worker) the update matches zero
        rows; the transaction is rolled back so that any pending changes made
        by this (now stale) worker are discarded, instead of being flushed and
        overwriting the current holder's result.

        :returns: ``True`` if the values were written.
        """
        table = cls.__table__
        stmt = (
            table.update()
            .where(table.c.id == object_id)
            .where(table.c.claim_token == token)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        return cls._run_fenced(stmt)

    @classmethod
    def _run_fenced(cls, stmt, values=None, instance=None):
        """Run a claim update and commit only if it matched a single row.

        A multi-worker race on a row is serialised by the database; the loser
        sees ``rowcount == 0`` and rolls back rather than committing work it
        no longer owns.

        When ``instance`` and ``values`` are given the instance's attributes
        are updated in memory only *after* a successful write, using
        ``set_committed_value`` so the ORM never treats them as pending changes
        (which a later commit could flush as an unguarded UPDATE).
        """
        result = Session.execute(stmt)
        matched = result.rowcount == 1
        if matched:
            Session.commit()
            if instance is not None and values:
                for key, value in values.items():
                    try:
                        set_committed_value(instance, key, value)
                    except Exception:  # pragma: no cover - detached
                        pass
        else:
            Session.rollback()
        return matched

    # Instance-level convenience wrappers. They operate on ``self.id``. The
    # guarded write also updates ``self`` (as committed values) on success.

    def claim(self, token, timeout):
        return self.claim_for(self.id, token, timeout)

    def renew_claim(self, token, timeout):
        return self.renew_claim_for(self.id, token, timeout)

    def release_claim(self, token):
        return self.release_claim_for(self.id, token)

    def save_claim_guarded(self, token, **values):
        table = self.__table__
        stmt = (
            table.update()
            .where(table.c.id == self.id)
            .where(table.c.claim_token == token)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        return self._run_fenced(stmt, values=values, instance=self)


class HarvestObjectExtra(BaseModel, HarvestDomainObject):
    """Extra key value data for Harvest objects"""

    __tablename__ = "harvest_object_extra"

    id = Column(types.UnicodeText, primary_key=True, default=make_uuid)
    harvest_object_id = Column(types.UnicodeText, ForeignKey("harvest_object.id"))
    key = Column(types.UnicodeText)
    value = Column(types.UnicodeText)
    harvest_object_id_idx = Index("harvest_object_id")
    object = relationship(
        HarvestObject, backref=backref("extras", cascade="all,delete-orphan")
    )


class HarvestGatherError(BaseModel, HarvestDomainObject):
    """Gather errors are raised during the **gather** stage of a harvesting
    job.
    """

    __tablename__ = "harvest_gather_error"

    id = Column(types.UnicodeText, primary_key=True, default=make_uuid)
    harvest_job_id = Column(types.UnicodeText, ForeignKey("harvest_job.id"))
    message = Column(types.UnicodeText)
    created = Column(types.DateTime, default=datetime.datetime.utcnow)

    job = relationship(HarvestJob, backref="gather_errors")

    @classmethod
    def create(cls, message, job):
        """
        Helper function to create an error object and save it.
        """
        err = cls(message=message, job=job)
        try:
            err.save()
        except InvalidRequestError:
            Session.rollback()
            err.save()
        finally:
            # No need to alert administrator so don't log as an error
            log.info(message)


class HarvestObjectError(BaseModel, HarvestDomainObject):
    """Object errors are raised during the **fetch** or **import** stage of a
    harvesting job, and are referenced to a specific harvest object.
    """

    __tablename__ = "harvest_object_error"

    id = Column(types.UnicodeText, primary_key=True, default=make_uuid)
    harvest_object_id = Column(types.UnicodeText, ForeignKey("harvest_object.id"))
    message = Column(types.UnicodeText)
    stage = Column(types.UnicodeText)
    line = Column(types.Integer)
    created = Column(types.DateTime, default=datetime.datetime.utcnow)
    harvest_error_harvest_object_id_idx = Index("harvest_object_id")

    object = relationship(
        HarvestObject, backref=backref("errors", cascade="all,delete-orphan")
    )

    @classmethod
    def create(cls, message, object, stage="Fetch", line=None):
        """
        Helper function to create an error object and save it.
        """
        err = cls(message=message, object=object, stage=stage, line=line)
        try:
            err.save()
        except InvalidRequestError:
            # Clear any in-progress sqlalchemy transactions
            try:
                Session.rollback()
            except Exception:
                pass
            try:
                Session.remove()
            except Exception:
                pass
            err.save()
        finally:
            log_message = "{0}, line {1}".format(message, line) if line else message
            log.debug(log_message)


class HarvestLog(BaseModel, HarvestDomainObject):
    """HarvestLog objects are created each time something is logged
    using python's standard logging module
    """

    __tablename__ = "harvest_log"

    id = Column(types.UnicodeText, primary_key=True, default=make_uuid)
    content = Column(types.UnicodeText, nullable=False)
    level = Column(
        types.Enum("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", name="log_level"),
    )
    created = Column(types.DateTime, default=datetime.datetime.utcnow)


def harvest_object_before_insert_listener(mapper, connection, target):
    """
    For compatibility with old harvesters, check if the source id has
    been set, and set it automatically from the job if not.
    """
    if not target.harvest_source_id or not target.source:
        if not target.job:
            raise Exception("You must define a Harvest Job for each Harvest Object")
        target.harvest_source_id = target.job.source.id


class PackageIdHarvestSourceIdMismatch(Exception):
    """
    The package created for the harvest source must match the id of the
    harvest source
    """

    pass


def clean_harvest_log(condition):
    Session.query(HarvestLog).filter(HarvestLog.created <= condition).delete(
        synchronize_session=False
    )
    try:
        Session.commit()
    except InvalidRequestError:
        Session.rollback()
        log.error("An error occurred while trying to clean-up the harvest log table")

    log.info("Harvest log table clean-up finished successfully")


event.listen(HarvestObject, "before_insert", harvest_object_before_insert_listener)
