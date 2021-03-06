#Standard python libraries
import sys
import datetime
from datetime import timedelta

#Specific python libraries
import sqlalchemy
from sqlalchemy.orm import sessionmaker
from sqlalchemy import exc, func

#PanDA server libraries
from config import panda_config
from pandalogger.PandaLogger import PandaLogger

#Configurator libraries
from models import Site, PandaSite, DdmEndpoint, Schedconfig, Jobsactive4, SiteStats

#Read connection parameters
__host = panda_config.dbhost
__user = panda_config.dbuser
__passwd = panda_config.dbpasswd
__dbname = panda_config.dbname

#Instantiate logger
_logger = PandaLogger().getLogger('configurator_dbif')

#Log the SQL produced by SQLAlchemy
__echo = True

#Create the SQLAlchemy engine
try:
    __engine = sqlalchemy.create_engine("oracle://%s:%s@%s"%(__user, __passwd, __host), 
                                         echo=__echo)
except exc.SQLAlchemyError:
    _logger.critical("Could not load the DB engine: %s"%sys.exc_info())
    raise


def get_session():
    return sessionmaker(bind=__engine)()


def db_interaction(method):
    """
    NOTE: THIS FUNCTION IS A REMAINDER FROM PREVIOUS DEVELOPMENT AND IS NOT CURRENTLY USED,
    BUT SHOULD BE ADAPTED TO REMOVE THE BOILERPLATE CODE IN ALL FUNCTIONS BELOW
    Decorator to wrap a function with the necessary session handling.
    FIXME: Getting a session has a 50ms overhead. See if there are better ways.
    FIXME: Note that this method inserts the session as the first parameter of the function. There might
        be more elegant solutions to do this.
    """
    def session_wrapper(*args, **kwargs):
        try:
            session = sessionmaker(bind=__engine)()
            result = method(session, *args, **kwargs)
            session.commit()
            return result
        except exc.SQLAlchemyError:
            _logger.critical("db_session excepted with error: %s"%sys.exc_info())
            session.rollback()
            raise
    return session_wrapper

# TODO: The performance of all write methods could significantly be improved by writing in bulks.
# The current implementation was the fastest way to get it done with the merge method and avoiding
# issues with duplicate keys
def write_sites_db(session, sites_list):
    """
    Cache the AGIS site information in the PanDA database
    """
    try:
        _logger.debug("Starting write_sites_db")
        for site in sites_list:
            _logger.debug("Site: {0}".format(site['site_name']))
            session.merge(Site(site_name = site['site_name'], 
                                      role = site['role'],
                                      tier_level = site['tier_level']))
        session.commit()
        _logger.debug("Done with write_sites_db")
    except exc.SQLAlchemyError:
        session.rollback()
        _logger.critical('write_sites_db: Could not persist information --> {0}'.format(sys.exc_info()))


def write_panda_sites_db(session, panda_sites_list):
    """
    Cache the AGIS panda site information in the PanDA database
    """
    _logger.debug("Starting write_panda_sites_db")
    for panda_site in panda_sites_list:
        try:
            _logger.debug("panda_site: {0}".format(panda_site['panda_site_name']))
            session.merge(PandaSite(panda_site_name = panda_site['panda_site_name'],
                                    site_name = panda_site['site_name'],
                                    default_ddm_endpoint = panda_site['default_ddm_endpoint'],
                                    storage_site_name = panda_site['storage_site_name'],
                                    is_local = panda_site['is_local']))
            session.commit()
            _logger.debug("Done with write_panda_sites_db")
        except exc.SQLAlchemyError:
            session.rollback()
            _logger.critical('write_panda_sites_db: Could not persist information --> {0}'.format(sys.exc_info()))


def write_ddm_endpoints_db(session, ddm_endpoints_list):
    """
    Cache the AGIS ddm endpoints in the PanDA database
    """
    try:
        _logger.debug("Starting write_ddm_endpoints_db")
        for ddm_endpoint in ddm_endpoints_list:
            session.merge(DdmEndpoint(ddm_endpoint_name = ddm_endpoint['ddm_endpoint_name'], 
                                        site_name = ddm_endpoint['site_name'], 
                                        ddm_spacetoken_name = ddm_endpoint['ddm_spacetoken_name'],
                                        type = ddm_endpoint['type'],
                                        is_tape = ddm_endpoint['is_tape'],
                                        blacklisted = ddm_endpoint['blacklisted'],
                                        space_used = ddm_endpoint['space_used'],
                                        space_free = ddm_endpoint['space_free'],
                                        space_total = ddm_endpoint['space_total'],
                                        space_expired = ddm_endpoint['space_expired'],
                                        space_timestamp = ddm_endpoint['space_timestamp']
                                        ))
        session.commit()
        _logger.debug("Done with write_ddm_endpoints_db")
    except exc.SQLAlchemyError:
        session.rollback()
        _logger.critical('write_ddm_endpoints_db: Could not persist information --> {0}'.format(sys.exc_info()))


def read_panda_ddm_relationships_schedconfig(session):
    """
    Read the PanDA - DDM relationships from schedconfig
    """
    try:
        _logger.debug("Starting read_panda_ddm_relationships_schedconfig")
        schedconfig = session.query(Schedconfig.site, Schedconfig.siteid, Schedconfig.ddm).all()
        relationship_tuples = []
        for entry in schedconfig:
            site = entry.site
            panda_site = entry.siteid
            # Schedconfig stores DDM endpoints as a comma separated string. Strip just in case
            if entry.ddm:
                ddm_endpoints = [ddm_endpoint.strip() for ddm_endpoint in entry.ddm.split(',')]
            # Return the tuples and let the caller mingle it the way he wants
            relationship_tuples.append((site, panda_site, ddm_endpoints))
        _logger.debug("Done with read_panda_ddm_relationships_schedconfig")
        return relationship_tuples
    except exc.SQLAlchemyError:
        session.rollback()
        _logger.critical('read_panda_ddm_relationships_schedconfig excepted --> {0}'.format(sys.exc_info()))
        return []


def read_configurator_sites(session):
    """
    Read the site names from the configurator tables
    """
    try:
        _logger.debug("Starting read_configurator_sites")
        site_object_list = session.query(Site.site_name).all()
        site_set = set([entry.site_name for entry in site_object_list])
        _logger.debug("Done with read_configurator_sites")
        return site_set
    except exc.SQLAlchemyError:
        session.rollback()
        _logger.critical('read_configurator_sites excepted --> {0}'.format(sys.exc_info()))
        return []


def read_configurator_panda_sites(session):
    """
    Read the panda site names from the configurator tables
    """
    try:
        _logger.debug("Starting read_configurator_panda_sites")
        panda_site_object_list = session.query(PandaSite.panda_site_name).all()
        panda_site_set = set([entry.panda_site_name for entry in panda_site_object_list])
        _logger.debug("Done with read_configurator_panda_sites")
        return panda_site_set
    except exc.SQLAlchemyError:
        session.rollback()
        _logger.critical('read_configurator_panda_sites excepted --> {0}'.format(sys.exc_info()))
        return []


def read_configurator_ddm_endpoints(session):
    """
    Read the DDM endpoint names from the configurator tables
    """
    try:
        _logger.debug("Starting read_configurator_ddm_endpoints")
        ddm_endpoint_object_list = session.query(DdmEndpoint.ddm_endpoint_name).all()
        ddm_endpoint_set = set([entry.ddm_endpoint_name for entry in ddm_endpoint_object_list])
        _logger.debug("Done with read_configurator_ddm_endpoints")
        return ddm_endpoint_set
    except exc.SQLAlchemyError:
        session.rollback()
        _logger.critical('read_configurator_ddm_endpoints excepted --> {0}'.format(sys.exc_info()))
        return []


def read_schedconfig_sites(session):
    """
    Read the site names from the schedconfig table
    """
    try:
        _logger.debug("Starting read_schedconfig_sites")
        site_object_list = session.query(Schedconfig.site).all()
        site_set = set([entry.site for entry in site_object_list])
        _logger.debug("Done with read_schedconfig_sites")
        return site_set
    except exc.SQLAlchemyError:
        session.rollback()
        _logger.critical('read_schedconfig_sites excepted --> {0}'.format(sys.exc_info()))
        return []


def read_schedconfig_panda_sites(session):
    """
    Read the panda site names from the schedconfig table
    """
    try:
        _logger.debug("Starting read_schedconfig_panda_sites")
        panda_site_object_list = session.query(Schedconfig.siteid).all()
        panda_site_set = set([entry.siteid for entry in panda_site_object_list])
        _logger.debug("Done with read_schedconfig_panda_sites")
        return panda_site_set
    except exc.SQLAlchemyError:
        session.rollback()
        _logger.critical('read_schedconfig_panda_sites excepted --> {0}'.format(sys.exc_info()))
        return []


def update_storage(session, ddm_endpoint_name, rse_usage):
    """
    Updates the storage of a DDM endpoint
    """
    try:
        _logger.debug("Starting update_storage for {0} with usage {1}".format(ddm_endpoint_name, rse_usage))
        ddm_endpoint = session.query(DdmEndpoint).filter(DdmEndpoint.ddm_endpoint_name==ddm_endpoint_name).one()
        ddm_endpoint.space_total = rse_usage['total']
        ddm_endpoint.space_free = rse_usage['free']
        ddm_endpoint.space_used = rse_usage['used']
        ddm_endpoint.space_expired = rse_usage['expired']
        ddm_endpoint.space_timestamp = rse_usage['space_timestamp']
        session.commit()
        _logger.debug("Done with update_storage")
    except exc.SQLAlchemyError:
        session.rollback()
        _logger.critical('update_storage excepted --> {0}'.format(sys.exc_info()))


def delete_sites(session, sites_to_delete):
    """
    Delete sites and all dependent entries (panda_sites, ddm_endpoints, panda_ddm_relations).
    Deletion of dependent entries is done through cascade definition in models 
    """
    if not sites_to_delete:
        _logger.debug("delete_sites: nothing to delete")
        return
    site_objects = session.query(Site).filter(Site.site_name.in_(sites_to_delete)).all()
    for site_object in site_objects:
        site_name = site_object.site_name
        try:
            _logger.debug('Going to delete site  --> {0}'.format(site_name))
            session.delete(site_object)
            session.commit()
            _logger.debug('Deleted site  --> {0}'.format(site_name))
        except exc.SQLAlchemyError:
            session.rollback()
            _logger.critical('delete_sites excepted for site {0} with {1}'.format(site_name, sys.exc_info()))
    return


def delete_panda_sites(session, panda_sites_to_delete):
    """
    Delete PanDA sites and dependent entries in panda_ddm_relations 
    """
    if not panda_sites_to_delete:
        _logger.debug("delete_panda_sites: nothing to delete")
        return
    panda_site_objects = session.query(PandaSite).filter(PandaSite.panda_site_name.in_(panda_sites_to_delete)).all()
    for panda_site_object in panda_site_objects:
        panda_site_name = panda_site_object.panda_site_name
        try:
            _logger.debug('Going to delete panda_site  --> {0}'.format(panda_site_name))
            session.delete(panda_site_object)
            session.commit()
            _logger.debug('Deleted panda_site  --> {0}'.format(panda_site_name))
        except exc.SQLAlchemyError:
            session.rollback()
            _logger.critical('delete_panda_sites excepted for panda_site {0} with {1}'.format(panda_site_name, sys.exc_info()))


def delete_ddm_endpoints(session, ddm_endpoints_to_delete):
    """
    Delete DDM endpoints dependent entries in panda_ddm_relations
    """
    if not ddm_endpoints_to_delete:
        _logger.debug("delete_ddm_endpoints: nothing to delete")
        return
    ddm_endpoint_objects = session.query(DdmEndpoint).filter(DdmEndpoint.ddm_endpoint_name.in_(ddm_endpoints_to_delete)).all()
    for ddm_endpoint_object in ddm_endpoint_objects:
        ddm_endpoint_name = ddm_endpoint_object.ddm_endpoint_name
        try:
            _logger.debug('Going to delete ddm_endpoint  --> {0}'.format(ddm_endpoint_name))
            session.delete(ddm_endpoint_object)
            session.commit()
            _logger.debug('Deleted ddm_endpoint  --> {0}'.format(ddm_endpoint_name))
        except exc.SQLAlchemyError:
            session.rollback()
            _logger.critical('delete_ddm_endpoints excepted for ddm_endpoint {0} with {1}'.format(ddm_endpoint_name, sys.exc_info()))


def get_cores_by_site(session):
    """
    Gets the number of cores by site
    """
    # SELECT sysdate, ps.site_name, VO, SUM(corecount) AS num_cores
    # FROM ATLAS_PANDA.jobsActive4 ja4, atlas_panda.panda_site ps
    # WHERE jobstatus in ('sent', 'starting', 'running', 'holding')
    # AND ps.panda_site_name = ja4.computingsite
    # GROUP BY sysdate, ps.site_name, VO

    try:
        for ts, site_name, core_count in session.query(func.sysdate(), PandaSite.site_name, func.sum(Jobsactive4.corecount)).\
                                                   filter(Jobsactive4.computingsite==PandaSite.panda_site_name).\
                                                   filter(Jobsactive4.jobstatus=='running').\
                                                   group_by(func.sysdate(), PandaSite.site_name).all():
            try:
                site_stat = SiteStats(site_name=site_name, ts=ts, key='total_corepower', value=core_count)
                session.add(site_stat)
                session.commit()
            except exc.SQLAlchemyError:
                session.rollback()
                _logger.critical('get_cores_by_site excepted when adding new site stats (site_name: {0}, ts: {1}, key: corecount, value: {2}). Exception {3}'.format(site_name, ts, core_count, sys.exc_info()))

    except exc.SQLAlchemyError:
        session.rollback()
        _logger.critical('get_cores_by_site excepted querying the corecount by sites with {0}'.format(sys.exc_info()))


def clean_site_stats_key(session, key, days=7):
    """
    Cleans entries older than days
    """
    ts_limit = datetime.datetime.now() - timedelta(days=7)
    try:
        entries_to_remove = session.query(SiteStats).filter(SiteStats.ts<ts_limit).filter(SiteStats.key==key).all()
        for entry_to_remove in entries_to_remove:
            try:
                session.delete(entry_to_remove)
                session.commit()
            except exc.SQLAlchemyError:
                session.rollback()
                _logger.critical('clean_site_stats_key excepted when deleting entry {0} with: {1}'.format(entry_to_remove, sys.exc_info()))
    except exc.SQLAlchemyError:
        session.rollback()
        _logger.critical('clean_site_stats_key excepted querying stats to remove: {0}'.format(sys.exc_info()))
