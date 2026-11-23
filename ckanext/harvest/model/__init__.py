import logging
import datetime

from sqlalchemy import event
from sqlalchemy import Column
from sqlalchemy import ForeignKey
from sqlalchemy import types
from sqlalchemy import Index
from sqlalchemy import UniqueConstraint
from sqlalchemy.orm import backref, relationship
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


class HarvestConfigVersion(BaseModel, HarvestDomainObject):
    """A versioned snapshot of a harvest source's configuration.

    Each harvest job pins the config version that was confirmed when the
    job was created, so that all objects gathered in the same job are
    processed with the same configuration, even if the source is edited
    (e.g. its default owner org or field rules) while the job is still
    queued.

    The snapshot currently covers:

    * ``config``: the source configuration (JSON string), as stored in
      ``harvest_source.config``
    * ``owner_org``: the default owner organisation of the source dataset
      at the time the version was confirmed

    Versions are deduplicated: as long as the source configuration does
    not change, new jobs keep pointing to the same version row.
    """

    __tablename__ = "harvest_config_version"

    id = Column(types.UnicodeText, primary_key=True, default=make_uuid)
    source_id = Column(
        types.UnicodeText,
        ForeignKey("harvest_source.id", ondelete="CASCADE"),
        nullable=False,
    )
    version = Column(types.Integer, nullable=False)
    config = Column(types.UnicodeText, default="")
    owner_org = Column(types.UnicodeText, nullable=True)
    created = Column(types.DateTime, default=datetime.datetime.utcnow)

    __table_args__ = (
        UniqueConstraint(
            "source_id", "version", name="harvest_config_version_source_version_key"
        ),
        Index("harvest_config_version_source_id_idx", "source_id"),
    )

    source = relationship(
        "HarvestSource",
        lazy="select",
        backref="config_versions",
    )

    jobs = relationship(
        "HarvestJob",
        lazy="select",
        back_populates="config_version",
    )

    def __repr__(self):
        return "<HarvestConfigVersion id=%s source_id=%s version=%s>" % (
            self.id,
            self.source_id,
            self.version,
        )

    @classmethod
    def get_or_create_for_source(cls, source):
        """Returns the config version matching the source's current state.

        If the latest recorded version for the source already matches its
        current configuration (config string and owner org), it is
        returned unchanged. Otherwise a new version is created and saved,
        so that the confirmed configuration stays traceable.
        """
        config = source.config or ""
        package = Package.get(source.id)
        owner_org = package.owner_org if package else None

        latest = (
            Session.query(cls)
            .filter(cls.source_id == source.id)
            .order_by(cls.version.desc())
            .first()
        )

        if (
            latest is not None
            and (latest.config or "") == config
            and (latest.owner_org or None) == (owner_org or None)
        ):
            return latest

        version = latest.version + 1 if latest is not None else 1
        config_version = cls(
            source_id=source.id,
            version=version,
            config=config,
            owner_org=owner_org,
        )
        config_version.save()
        log.info(
            "Confirmed config version %s for harvest source %s", version, source.id
        )
        return config_version


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
    # The config version confirmed when the job was created. NULL for jobs
    # created before config versioning existed (they fall back to the live
    # source configuration).
    config_version_id = Column(
        types.UnicodeText,
        ForeignKey("harvest_config_version.id", ondelete="SET NULL"),
        nullable=True,
    )
    source = relationship(
        "HarvestSource",
        lazy="select",
        back_populates="jobs",
    )
    config_version = relationship(
        "HarvestConfigVersion",
        lazy="select",
        back_populates="jobs",
    )

    # Transient, in-memory only override used by replay-style actions (e.g.
    # ``harvest_objects_import`` with ``config_version='current'``) to process
    # this job with a different config version than the pinned one. It is
    # never persisted to the database.
    _config_version_override = None

    def get_config_version(self):
        """Returns the config version this job is being processed with.

        This is normally the version pinned when the job was created, but
        replay actions may set a transient override. Returns None for jobs
        created before config versioning existed.
        """
        if self._config_version_override is not None:
            return self._config_version_override
        return self.config_version

    def get_config(self):
        """Returns the source configuration (JSON string) for this job.

        Uses the pinned config version if there is one, and falls back to
        the live source configuration otherwise.
        """
        config_version = self.get_config_version()
        if config_version is not None:
            return config_version.config
        if self.source is not None:
            return self.source.config
        return None

    def get_owner_org(self):
        """Returns the default owner org to assign harvested datasets to.

        Uses the owner org confirmed in the pinned config version if there
        is one, and falls back to the source dataset's live owner org
        otherwise.
        """
        config_version = self.get_config_version()
        if config_version is not None:
            return config_version.owner_org
        package = Package.get(self.source_id)
        return package.owner_org if package else None

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
