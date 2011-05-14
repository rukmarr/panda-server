import os
import re
import sys
import time
import fcntl
import types
import shelve
import random
import datetime
import commands
import threading
import userinterface.Client as Client
from dataservice.DDM import ddm
from dataservice.DDM import dashBorad
from taskbuffer.OraDBProxy import DBProxy
from taskbuffer.TaskBuffer import taskBuffer
from pandalogger.PandaLogger import PandaLogger
from jobdispatcher.Watcher import Watcher
from brokerage.SiteMapper import SiteMapper
from dataservice.Adder import Adder
from dataservice.Finisher import Finisher
from dataservice.MailUtils import MailUtils
from taskbuffer import ProcessGroups
import brokerage.broker_util
import brokerage.broker
import taskbuffer.ErrorCode
import dataservice.DDM

# password
from config import panda_config
passwd = panda_config.dbpasswd

# logger
_logger = PandaLogger().getLogger('datasetManager')

_logger.debug("===================== start =====================")

# memory checker
def _memoryCheck(str):
    try:
        proc_status = '/proc/%d/status' % os.getpid()
        procfile = open(proc_status)
        name   = ""
        vmSize = ""
        vmRSS  = ""
        # extract Name,VmSize,VmRSS
        for line in procfile:
            if line.startswith("Name:"):
                name = line.split()[-1]
                continue
            if line.startswith("VmSize:"):
                vmSize = ""
                for item in line.split()[1:]:
                    vmSize += item
                continue
            if line.startswith("VmRSS:"):
                vmRSS = ""
                for item in line.split()[1:]:
                    vmRSS += item
                continue
        procfile.close()
        _logger.debug('MemCheck - %s Name=%s VSZ=%s RSS=%s : %s' % (os.getpid(),name,vmSize,vmRSS,str))
    except:
        type, value, traceBack = sys.exc_info()
        _logger.error("memoryCheck() : %s %s" % (type,value))
        _logger.debug('MemCheck - %s unknown : %s' % (os.getpid(),str))
    return

_memoryCheck("start")

# kill old dq2 process
try:
    # time limit
    timeLimit = datetime.datetime.utcnow() - datetime.timedelta(hours=2)
    # get process list
    scriptName = sys.argv[0]
    out = commands.getoutput('ps axo user,pid,lstart,args | grep dq2.clientapi | grep -v PYTHONPATH | grep -v grep')
    for line in out.split('\n'):
        if line == '':
            continue
        items = line.split()
        # owned process
        if not items[0] in ['sm','atlpan','root']: # ['os.getlogin()']: doesn't work in cron
            continue
        # look for python
        if re.search('python',line) == None:
            continue
        # PID
        pid = items[1]
        # start time
        timeM = re.search('(\S+\s+\d+ \d+:\d+:\d+ \d+)',line)
        startTime = datetime.datetime(*time.strptime(timeM.group(1),'%b %d %H:%M:%S %Y')[:6])
        # kill old process
        if startTime < timeLimit:
            _logger.debug("old dq2 process : %s %s" % (pid,startTime))
            _logger.debug(line)            
            commands.getoutput('kill -9 %s' % pid)
except:
    type, value, traceBack = sys.exc_info()
    _logger.error("kill dq2 process : %s %s" % (type,value))


# kill old process
try:
    # time limit
    timeLimit = datetime.datetime.utcnow() - datetime.timedelta(hours=7)
    # get process list
    scriptName = sys.argv[0]
    out = commands.getoutput('ps axo user,pid,lstart,args | grep %s' % scriptName)
    for line in out.split('\n'):
        items = line.split()
        # owned process
        if not items[0] in ['sm','atlpan','root']: # ['os.getlogin()']: doesn't work in cron
            continue
        # look for python
        if re.search('python',line) == None:
            continue
        # PID
        pid = items[1]
        # start time
        timeM = re.search('(\S+\s+\d+ \d+:\d+:\d+ \d+)',line)
        startTime = datetime.datetime(*time.strptime(timeM.group(1),'%b %d %H:%M:%S %Y')[:6])
        # kill old process
        if startTime < timeLimit:
            _logger.debug("old process : %s %s" % (pid,startTime))
            _logger.debug(line)            
            commands.getoutput('kill -9 %s' % pid)
except:
    type, value, traceBack = sys.exc_info()
    _logger.error("kill process : %s %s" % (type,value))
    

# instantiate TB
taskBuffer.init(panda_config.dbhost,panda_config.dbpasswd,nDBConnection=1)

# instantiate sitemapper
siteMapper = SiteMapper(taskBuffer)

# delete old datasets
timeLimitDnS = datetime.datetime.utcnow() - datetime.timedelta(days=60)
timeLimitTop = datetime.datetime.utcnow() - datetime.timedelta(days=90)
nDelDS = 1000
for dsType,dsPrefix in [('','top'),]:
    sql = "DELETE FROM ATLAS_PANDA.Datasets "
    if dsType != '':
        # dis or sub
        sql += "WHERE type=:type AND modificationdate<:modificationdate "
        sql += "AND REGEXP_LIKE(name,:pattern) AND rownum <= %s" % nDelDS
        varMap = {}
        varMap[':modificationdate'] = timeLimitDnS
        varMap[':type'] = dsType
        varMap[':pattern'] = '_%s[[:digit:]]+$' % dsPrefix
    else:
        # top level datasets
        sql+= "WHERE modificationdate<:modificationdate AND rownum <= %s" % nDelDS
        varMap = {}
        varMap[':modificationdate'] = timeLimitTop
    for i in range(100):
        # del datasets
        ret,res = taskBuffer.querySQLS(sql, varMap)
        _logger.debug("# of %s datasets deleted: %s" % (dsPrefix,res))
        # no more datasets    
        if res != nDelDS:
            break

# thread pool
class ThreadPool:
    def __init__(self):
        self.lock = threading.Lock()
        self.list = []

    def add(self,obj):
        self.lock.acquire()
        self.list.append(obj)
        self.lock.release()

    def remove(self,obj):
        self.lock.acquire()
        self.list.remove(obj)
        self.lock.release()

    def join(self):
        self.lock.acquire()
        thrlist = tuple(self.list)
        self.lock.release()
        for thr in thrlist:
            thr.join()


# thread to close dataset
class CloserThr (threading.Thread):
    def __init__(self,lock,proxyLock,datasets,pool):
        threading.Thread.__init__(self)
        self.datasets   = datasets
        self.lock       = lock
        self.proxyLock  = proxyLock
        self.pool       = pool
        self.pool.add(self)
                                        
    def run(self):
        self.lock.acquire()
        try:
            # loop over all datasets
            for vuid,name,modDate in self.datasets:
                _logger.debug("Close %s %s" % (modDate,name))
                if not name.startswith('pandaddm_'):
                    status,out = ddm.DQ2.main('freezeDataset',name)
                else:
                    status,out = 0,''
                if status != 0 and out.find('DQFrozenDatasetException') == -1 and \
                       out.find("DQUnknownDatasetException") == -1 and out.find("DQSecurityException") == -1 and \
                       out.find("DQDeletedDatasetException") == -1 and out.find("DQUnknownDatasetException") == -1:
                    _logger.error(out)
                else:
                    self.proxyLock.acquire()
                    varMap = {}
                    varMap[':vuid'] = vuid
                    varMap[':status'] = 'completed'
                    taskBuffer.querySQLS("UPDATE ATLAS_PANDA.Datasets SET status=:status,modificationdate=CURRENT_DATE WHERE vuid=:vuid",
                                     varMap)
                    self.proxyLock.release()                    
                    if name.startswith('pandaddm_'):
                        continue
                    # count # of files
                    status,out = ddm.DQ2.main('getNumberOfFiles',name)
                    _logger.debug(out)                                            
                    if status != 0:
                        _logger.error(out)                            
                    else:
                        try:
                            nFile = int(out)
                            _logger.debug(nFile)
                            if nFile == 0:
                                # erase dataset
                                _logger.debug('erase %s' % name)
                                status,out = ddm.DQ2.main('eraseDataset',name)
                                _logger.debug(out)                            
                        except:
                            pass
        except:
            pass
        self.pool.remove(self)
        self.lock.release()

# close datasets
timeLimitU = datetime.datetime.utcnow() - datetime.timedelta(minutes=30)
timeLimitL = datetime.datetime.utcnow() - datetime.timedelta(days=3)
closeLock = threading.Semaphore(5)
closeProxyLock = threading.Lock()
closeThreadPool = ThreadPool()
while True:
    # lock
    closeLock.acquire()
    # get datasets
    closeProxyLock.acquire()
    varMap = {}
    varMap[':modificationdateU'] = timeLimitU
    varMap[':modificationdateL'] = timeLimitL    
    varMap[':type']   = 'output'
    varMap[':status'] = 'tobeclosed'
    sqlQuery = "type=:type AND status=:status AND (modificationdate BETWEEN :modificationdateL AND :modificationdateU) AND rownum <= 500"    
    proxyS = taskBuffer.proxyPool.getProxy()
    res = proxyS.getLockDatasets(sqlQuery,varMap)
    taskBuffer.proxyPool.putProxy(proxyS)
    if res == None:
        _logger.debug("# of datasets to be closed: %s" % res)
    else:
        _logger.debug("# of datasets to be closed: %s" % len(res))
    if res==None or len(res)==0:
        closeProxyLock.release()
        closeLock.release()
        break
    # release
    closeProxyLock.release()
    closeLock.release()
    # run thread
    closerThr = CloserThr(closeLock,closeProxyLock,res,closeThreadPool)
    closerThr.start()

closeThreadPool.join()


# thread to freeze dataset
class Freezer (threading.Thread):
    def __init__(self,lock,proxyLock,datasets,pool):
        threading.Thread.__init__(self)
        self.datasets   = datasets
        self.lock       = lock
        self.proxyLock  = proxyLock
        self.pool       = pool
        self.pool.add(self)
                                        
    def run(self):
        self.lock.acquire()
        try:
            for vuid,name,modDate in self.datasets:
                _logger.debug("start %s %s" % (modDate,name))
                self.proxyLock.acquire()
                retF,resF = taskBuffer.querySQLS("SELECT /*+ index(tab FILESTABLE4_DESTDBLOCK_IDX) */ lfn FROM ATLAS_PANDA.filesTable4 tab WHERE destinationDBlock=:destinationDBlock",
                                             {':destinationDBlock':name})
                self.proxyLock.release()
                if retF<0:
                    _logger.error("SQL error")
                else:
                    # no files in filesTable
                    if len(resF) == 0:
                        _logger.debug("freeze %s " % name)
                        if not name.startswith('pandaddm_'):
                            status,out = ddm.DQ2.main('freezeDataset',name)
                        else:
                            status,out = 0,''
                        if status != 0 and out.find('DQFrozenDatasetException') == -1 and \
                               out.find("DQUnknownDatasetException") == -1 and out.find("DQSecurityException") == -1 and \
                               out.find("DQDeletedDatasetException") == -1 and out.find("DQUnknownDatasetException") == -1:
                            _logger.error(out)
                        else:
                            self.proxyLock.acquire()
                            varMap = {}
                            varMap[':vuid'] = vuid
                            varMap[':status'] = 'completed' 
                            taskBuffer.querySQLS("UPDATE ATLAS_PANDA.Datasets SET status=:status,modificationdate=CURRENT_DATE WHERE vuid=:vuid",
                                             varMap)
                            self.proxyLock.release()                            
                            if name.startswith('pandaddm_'):
                                continue
                            # count # of files
                            status,out = ddm.DQ2.main('getNumberOfFiles',name)
                            _logger.debug(out)                                            
                            if status != 0:
                                _logger.error(out)                            
                            else:
                                try:
                                    nFile = int(out)
                                    _logger.debug(nFile)
                                    if nFile == 0:
                                        # erase dataset
                                        _logger.debug('erase %s' % name)                                
                                        status,out = ddm.DQ2.main('eraseDataset',name)
                                        _logger.debug(out)                                                                
                                except:
                                    pass
                    else:
                        _logger.debug("wait %s " % name)
                        self.proxyLock.acquire()                        
                        taskBuffer.querySQLS("UPDATE ATLAS_PANDA.Datasets SET modificationdate=CURRENT_DATE WHERE vuid=:vuid", {':vuid':vuid})
                        self.proxyLock.release()                                                    
                _logger.debug("end %s " % name)
        except:
            pass
        self.pool.remove(self)
        self.lock.release()
                            
# freeze dataset
timeLimitU = datetime.datetime.utcnow() - datetime.timedelta(days=4)
timeLimitL = datetime.datetime.utcnow() - datetime.timedelta(days=14)
freezeLock = threading.Semaphore(5)
freezeProxyLock = threading.Lock()
freezeThreadPool = ThreadPool()
while True:
    # lock
    freezeLock.acquire()
    # get datasets
    sqlQuery = "type=:type AND status IN (:status1,:status2,:status3) " + \
               "AND (modificationdate BETWEEN :modificationdateL AND :modificationdateU) AND REGEXP_LIKE(name,:pattern) AND rownum <= 500"
    varMap = {}
    varMap[':modificationdateU'] = timeLimitU
    varMap[':modificationdateL'] = timeLimitL    
    varMap[':type'] = 'output'
    varMap[':status1'] = 'running'
    varMap[':status2'] = 'created'
    varMap[':status3'] = 'defined'
    varMap[':pattern'] = '_sub[[:digit:]]+$'
    freezeProxyLock.acquire()
    proxyS = taskBuffer.proxyPool.getProxy()
    res = proxyS.getLockDatasets(sqlQuery,varMap)
    taskBuffer.proxyPool.putProxy(proxyS)
    if res == None:
        _logger.debug("# of datasets to be frozen: %s" % res)
    else:
        _logger.debug("# of datasets to be frozen: %s" % len(res))
    if res==None or len(res)==0:
        freezeProxyLock.release()
        freezeLock.release()
        break
    freezeProxyLock.release()            
    # release
    freezeLock.release()
    # run freezer
    freezer = Freezer(freezeLock,freezeProxyLock,res,freezeThreadPool)
    freezer.start()

freezeThreadPool.join()


# thread to delete dataset replica from T2
class T2Cleaner (threading.Thread):
    def __init__(self,lock,proxyLock,datasets,pool):
        threading.Thread.__init__(self)
        self.datasets   = datasets
        self.lock       = lock
        self.proxyLock  = proxyLock
        self.pool       = pool
        self.pool.add(self)
                                        
    def run(self):
        self.lock.acquire()
        try:
            for vuid,name,modDate in self.datasets:
                _logger.debug("cleanT2 %s" % name)
                # get list of replicas
                status,out = ddm.DQ2.main('listDatasetReplicas',name,0,None,False)
                if status != 0 and out.find('DQFrozenDatasetException')  == -1 and \
                       out.find("DQUnknownDatasetException") == -1 and out.find("DQSecurityException") == -1 and \
                       out.find("DQDeletedDatasetException") == -1 and out.find("DQUnknownDatasetException") == -1:
                    _logger.error(out)
                    continue
                else:
                    try:
                        # convert res to map
                        exec "tmpRepSites = %s" % out
                    except:
                        tmpRepSites = {}
                        _logger.error("cannot convert to replica map")
                        _logger.error(out)
                        continue
                    # check cloud
                    cloudName = None
                    for tmpCloudName in siteMapper.getCloudList():
                        t1SiteName = siteMapper.getCloud(tmpCloudName)['source']
                        t1SiteDDMs  = siteMapper.getSite(t1SiteName).setokens.values()
                        for tmpDDM in t1SiteDDMs:
                            if tmpRepSites.has_key(tmpDDM):
                                cloudName = tmpCloudName
                                break
                    # cloud is not found
                    if cloudName == None:        
                        _logger.error("cannot find cloud for %s : %s" % (name,str(tmpRepSites)))
                    elif not cloudName in ['DE','CA','ES','FR','IT','NL','UK','TW']:
                        # FIXME : test only EGEE for now
                        pass
                    else:
                        # look for T2 IDs
                        t2DDMs = []
                        for tmpDDM in tmpRepSites.keys():
                            if not tmpDDM in t1SiteDDMs and tmpDDM.endswith('_PRODDISK'):
                                t2DDMs.append(tmpDDM)
                        # delete replica for sub
                        if re.search('_sub\d+$',name) != None and t2DDMs != []:
                            _logger.debug(('deleteDatasetReplicas',name,t2DDMs))
                            status,out = ddm.DQ2.main('deleteDatasetReplicas',name,t2DDMs)
                            if status != 0:
                                _logger.error(out)
                                if out.find('DQFrozenDatasetException')  == -1 and \
                                       out.find("DQUnknownDatasetException") == -1 and out.find("DQSecurityException") == -1 and \
                                       out.find("DQDeletedDatasetException") == -1 and out.find("DQUnknownDatasetException") == -1 and \
                                       out.find("No replica found") == -1:
                                    continue
                    # update        
                    self.proxyLock.acquire()
                    varMap = {}
                    varMap[':vuid'] = vuid
                    varMap[':status'] = 'completed' 
                    taskBuffer.querySQLS("UPDATE ATLAS_PANDA.Datasets SET status=:status,modificationdate=CURRENT_DATE WHERE vuid=:vuid",
                                         varMap)
                    self.proxyLock.release()                            
                _logger.debug("end %s " % name)
        except:
            pass
        self.pool.remove(self)
        self.lock.release()
                            
# delete dataset replica from T2
timeLimitU = datetime.datetime.utcnow() - datetime.timedelta(minutes=30)
timeLimitL = datetime.datetime.utcnow() - datetime.timedelta(days=3)
t2cleanLock = threading.Semaphore(5)
t2cleanProxyLock = threading.Lock()
t2cleanThreadPool = ThreadPool()
while True:
    # lock
    t2cleanLock.acquire()
    # get datasets
    varMap = {}
    varMap[':modificationdateU'] = timeLimitU
    varMap[':modificationdateL'] = timeLimitL    
    varMap[':type']   = 'output'
    varMap[':status'] = 'cleanup'
    sqlQuery = "type=:type AND status=:status AND (modificationdate BETWEEN :modificationdateL AND :modificationdateU) AND rownum <= 500"    
    t2cleanProxyLock.acquire()
    proxyS = taskBuffer.proxyPool.getProxy()
    res = proxyS.getLockDatasets(sqlQuery,varMap)
    taskBuffer.proxyPool.putProxy(proxyS)
    if res == None:
        _logger.debug("# of datasets to be deleted from T2: %s" % res)
    else:
        _logger.debug("# of datasets to be deleted from T2: %s" % len(res))
    if res==None or len(res)==0:
        t2cleanProxyLock.release()
        t2cleanLock.release()
        break
    t2cleanProxyLock.release()            
    # release
    t2cleanLock.release()
    # run t2cleanr
    t2cleanr = T2Cleaner(t2cleanLock,t2cleanProxyLock,res,t2cleanThreadPool)
    t2cleanr.start()

t2cleanThreadPool.join()


_memoryCheck("rebroker")

# rebrokerage
_logger.debug("Rebrokerage start")
try:
    sql  = "SELECT jobDefinitionID,prodUserName,prodUserID FROM ATLAS_PANDA.jobsActive4 "
    sql += "WHERE prodSourceLabel IN (:prodSourceLabel1,:prodSourceLabel2) AND jobStatus=:jobStatus "
    sql += "AND modificationTime<:modificationTime "
    sql += "AND jobsetID IS NOT NULL "    
    sql += "AND processingType IN (:processingType1,:processingType2) "
    sql += "GROUP BY jobDefinitionID,prodUserName,prodUserID " 
    varMap = {}
    varMap[':prodSourceLabel1'] = 'user'
    varMap[':prodSourceLabel2'] = 'panda'
    varMap[':modificationTime'] = datetime.datetime.utcnow() - datetime.timedelta(days=1)
    varMap[':processingType1']  = 'pathena'
    varMap[':processingType2']  = 'prun'
    varMap[':jobStatus']        = 'activated'
    # get jobs older than 1 days
    ret,res = taskBuffer.querySQLS(sql, varMap)
    sql  = "SELECT PandaID,modificationTime FROM %s WHERE prodUserName=:prodUserName AND jobDefinitionID=:jobDefinitionID "
    sql += "AND modificationTime>:modificationTime AND rownum <= 1"
    if res != None:
        from userinterface.ReBroker import ReBroker
        timeLimit = datetime.datetime.utcnow() - datetime.timedelta(hours=3)
        # loop over all user/jobID combinations
        for jobDefinitionID,prodUserName,prodUserID in res:
            # check if jobs with the jobID have run recently
            varMap = {}
            varMap[':prodUserName']     = prodUserName
            varMap[':jobDefinitionID']  = jobDefinitionID
            varMap[':modificationTime'] = timeLimit
            _logger.debug(" rebro:%s:%s" % (jobDefinitionID,prodUserName))
            hasRecentJobs = False
            for tableName in ['ATLAS_PANDA.jobsActive4','ATLAS_PANDA.jobsArchived4']: 
                retU,resU = taskBuffer.querySQLS(sql % tableName, varMap)
                if resU == None:
                    # database error
                    raise RuntimeError,"failed to check modTime"
                if resU != []:
                    # found recent jobs
                    hasRecentJobs = True
                    _logger.debug("    -> skip %s ran recently at %s" % (resU[0][0],resU[0][1]))
                    break
            if hasRecentJobs:    
                # skip since some jobs have run recently
                continue
            else:
                reBroker = ReBroker(taskBuffer)
                # try to lock
                rebRet,rebOut = reBroker.lockJob(prodUserID,jobDefinitionID)
                if not rebRet:
                    # failed to lock
                    _logger.debug("    -> failed to lock : %s" % rebOut)
                    continue
                else:
                    # start
                    _logger.debug("    -> start")
                    reBroker.start()
                    reBroker.join()
except:
    errType,errValue = sys.exc_info()[:2]
    _logger.error("rebrokerage failed with %s:%s" % (errType,errValue))


_memoryCheck("finisher")

# finish transferring jobs
timeNow   = datetime.datetime.utcnow()
timeLimit = datetime.datetime.utcnow() - datetime.timedelta(hours=12)
sql = "SELECT PandaID FROM ATLAS_PANDA.jobsActive4 WHERE jobStatus=:jobStatus AND modificationTime<:modificationTime AND rownum<=20"
for ii in range(1000):
    varMap = {}
    varMap[':jobStatus'] = 'transferring'
    varMap[':modificationTime'] = timeLimit
    ret,res = taskBuffer.querySQLS(sql, varMap)
    if res == None:
        _logger.debug("# of jobs to be finished : %s" % res)
        break
    else:
        _logger.debug("# of jobs to be finished : %s" % len(res))
        if len(res) == 0:
            break
        # get jobs from DB
        ids = []
        for (id,) in res:
            ids.append(id)
        jobs = taskBuffer.peekJobs(ids,fromDefined=False,fromArchived=False,fromWaiting=False)
        # update modificationTime to lock jobs
        for job in jobs:
            if job != None and job.jobStatus != 'unknown':
                taskBuffer.updateJobStatus(job.PandaID,job.jobStatus,{})
        upJobs = []
        finJobs = []
        for job in jobs:
            if job == None or job.jobStatus == 'unknown':
                continue
            # use BNL by default
            dq2URL = siteMapper.getSite('BNL_ATLAS_1').dq2url
            dq2SE  = []
            # get LFC and SEs
            if job.prodSourceLabel == 'user' and not siteMapper.siteSpecList.has_key(job.destinationSE):
                # using --destSE for analysis job to transfer output
                try:
                    dq2URL = dataservice.DDM.toa.getLocalCatalog(job.destinationSE)[-1]
                    match = re.search('.+://([^:/]+):*\d*/*',dataservice.DDM.toa.getSiteProperty(job.destinationSE,'srm')[-1])
                    if match != None:
                        dq2SE.append(match.group(1))
                except:
                    type, value, traceBack = sys.exc_info()
                    _logger.error("Failed to get DQ2/SE for %s with %s %s" % (job.PandaID,type,value))
                    continue
            elif siteMapper.checkCloud(job.cloud):
                # normal production jobs
                tmpDstID   = siteMapper.getCloud(job.cloud)['dest']
                tmpDstSite = siteMapper.getSite(tmpDstID)
                if not tmpDstSite.lfchost in [None,'']:
                    # LFC
                    dq2URL = 'lfc://'+tmpDstSite.lfchost+':/grid/atlas/'
                    if tmpDstSite.se != None:
                        for tmpDstSiteSE in tmpDstSite.se.split(','):
                            match = re.search('.+://([^:/]+):*\d*/*',tmpDstSiteSE)
                            if match != None:
                                dq2SE.append(match.group(1))
                else:
                    # LRC
                    dq2URL = tmpDstSite.dq2url
                    dq2SE  = []
            # get LFN list
            lfns  = []
            guids = []
            nTokens = 0
            for file in job.Files:
                # only output files are checked
                if file.type == 'output' or file.type == 'log':
                    lfns.append(file.lfn)
                    guids.append(file.GUID)
                    nTokens += len(file.destinationDBlockToken.split(','))
            # get files in LRC
            _logger.debug("Cloud:%s DQ2URL:%s" % (job.cloud,dq2URL))
            okFiles = brokerage.broker_util.getFilesFromLRC(lfns,dq2URL,guids,dq2SE,getPFN=True)
            # count files
            nOkTokens = 0
            for okLFN,okPFNs in okFiles.iteritems():
                nOkTokens += len(okPFNs)
            # check all files are ready    
            _logger.debug(" nToken:%s nOkToken:%s" % (nTokens,nOkTokens))
            if nTokens <= nOkTokens:
                _logger.debug("Finisher : Finish %s" % job.PandaID)
                for file in job.Files:
                    if file.type == 'output' or file.type == 'log':
                        file.status = 'ready'
                # append to run Finisher
                finJobs.append(job)                        
            else:
                endTime = job.endTime
                if endTime == 'NULL':
                    endTime = job.startTime
                # priority-dependent timeout
                tmpCloudSpec = siteMapper.getCloud(job.cloud)
                if job.currentPriority >= 900 and (not job.prodSourceLabel in ['user']):
                    if tmpCloudSpec.has_key('transtimehi'):
                        timeOutValue = tmpCloudSpec['transtimehi']
                    else:
                        timeOutValue = 1
                else:
                    if tmpCloudSpec.has_key('transtimelo'):                    
                        timeOutValue = tmpCloudSpec['transtimelo']
                    else:
                        timeOutValue = 2                        
                # protection
                if timeOutValue < 1:
                    timeOutValue  = 1
                timeOut = timeNow - datetime.timedelta(days=timeOutValue)
                _logger.debug("  Priority:%s Limit:%s End:%s" % (job.currentPriority,str(timeOut),str(endTime)))
                if endTime < timeOut:
                    # timeout
                    _logger.debug("Finisher : Kill %s" % job.PandaID)
                    strMiss = ''
                    for lfn in lfns:
                        if not lfn in okFiles:
                            strMiss += ' %s' % lfn
                    job.jobStatus = 'failed'
                    job.taskBufferErrorCode = taskbuffer.ErrorCode.EC_Transfer
                    job.taskBufferErrorDiag = 'transfer timeout for '+strMiss
                    guidMap = {}
                    for file in job.Files:
                        # set file status
                        if file.status == 'transferring':
                            file.status = 'failed'
                        # collect GUIDs to delete files from _tid datasets
                        if file.type == 'output' or file.type == 'log':
                            if not guidMap.has_key(file.destinationDBlock):
                                guidMap[file.destinationDBlock] = []
                            guidMap[file.destinationDBlock].append(file.GUID)
                else:
                    # wait
                    _logger.debug("Finisher : Wait %s" % job.PandaID)
                    for lfn in lfns:
                        if not lfn in okFiles:
                            _logger.debug("    -> %s" % lfn)
            upJobs.append(job)
        # update
        _logger.debug("updating ...")
        taskBuffer.updateJobs(upJobs,False)
        # run Finisher
        for job in finJobs:
            fThr = Finisher(taskBuffer,None,job)
            fThr.start()
            fThr.join()
        _logger.debug("done")
        time.sleep(random.randint(1,10))

                    
# update email DB        
_memoryCheck("email")
_logger.debug("Update emails")

# lock file
_lockGetMail = open(panda_config.lockfile_getMail, 'w')
# lock email DB
fcntl.flock(_lockGetMail.fileno(), fcntl.LOCK_EX)
# open email DB
pDB = shelve.open(panda_config.emailDB)
# read
mailMap = {}
for name,addr in pDB.iteritems():
    mailMap[name] = addr
# close DB
pDB.close()
# release file lock
fcntl.flock(_lockGetMail.fileno(), fcntl.LOCK_UN)
# set email address
for name,addr in mailMap.iteritems():
    # remove _
    name = re.sub('_$','',name)
    status,res = taskBuffer.querySQLS("SELECT email FROM ATLAS_PANDAMETA.users WHERE name=:name",{':name':name})
    # failed or not found
    if status == -1 or len(res) == 0:
        _logger.error("%s not found in user DB" % name)
        continue
    # already set
    if not res[0][0] in ['','None',None]:
        continue
    # update email
    _logger.debug("set '%s' to %s" % (name,addr))
    status,res = taskBuffer.querySQLS("UPDATE ATLAS_PANDAMETA.users SET email=:addr WHERE name=:name",{':addr':addr,':name':name})

# reassign reprocessing jobs in defined table
_memoryCheck("repro")
class ReassginRepro (threading.Thread):
    def __init__(self,taskBuffer,lock,jobs):
        threading.Thread.__init__(self)
        self.jobs       = jobs
        self.lock       = lock
        self.taskBuffer = taskBuffer

    def run(self):
        self.lock.acquire()
        try:
            if len(self.jobs):
                nJob = 100
                iJob = 0
                while iJob < len(self.jobs):
                    # reassign jobs one by one to break dis dataset formation
                    for job in self.jobs[iJob:iJob+nJob]:
                        _logger.debug('reassignJobs in Pepro (%s)' % [job])
                        self.taskBuffer.reassignJobs([job],joinThr=True)
                    iJob += nJob
        except:
            pass
        self.lock.release()
        
reproLock = threading.Semaphore(3)

nBunch = 20
iBunch = 0
timeLimitMod = datetime.datetime.utcnow() - datetime.timedelta(hours=8)
timeLimitCre = datetime.datetime.utcnow() - datetime.timedelta(hours=24)
firstFlag = True
while True:
    # lock
    reproLock.acquire()
    # get jobs
    varMap = {}
    varMap[':jobStatus'] = 'assigned'
    varMap[':prodSourceLabel'] = 'managed'
    varMap[':modificationTime'] = timeLimitMod
    varMap[':creationTime'] = timeLimitCre
    varMap[':processingType'] = 'reprocessing'
    if firstFlag:
        firstFlag = False
        status,res = taskBuffer.querySQLS("SELECT PandaID FROM ATLAS_PANDA.jobsDefined4 WHERE jobStatus=:jobStatus AND prodSourceLabel=:prodSourceLabel AND modificationTime<:modificationTime AND creationTime<:creationTime AND processingType=:processingType ORDER BY PandaID",
                                      varMap)
        if res != None:
            _logger.debug('total Repro for reassignJobs : %s' % len(res))
    # get a bunch    
    status,res = taskBuffer.querySQLS("SELECT * FROM (SELECT PandaID FROM ATLAS_PANDA.jobsDefined4 WHERE jobStatus=:jobStatus AND prodSourceLabel=:prodSourceLabel AND modificationTime<:modificationTime AND creationTime<:creationTime AND processingType=:processingType ORDER BY PandaID) WHERE rownum<=%s" % nBunch,
                                  varMap)
    # escape
    if res == None or len(res) == 0:
        reproLock.release()
        break

    # get IDs
    jobs=[]
    for id, in res:
        jobs.append(id)
        
    # reassign
    _logger.debug('reassignJobs for Pepro %s' % (iBunch*nBunch))
    # lock
    currentTime = datetime.datetime.utcnow()
    for jobID in jobs:
        varMap = {}
        varMap[':PandaID'] = jobID
        varMap[':modificationTime'] = currentTime
        status,res = taskBuffer.querySQLS("UPDATE ATLAS_PANDA.jobsDefined4 SET modificationTime=:modificationTime WHERE PandaID=:PandaID",
                                          varMap)
    reproLock.release()
    # run thr
    reproThr = ReassginRepro(taskBuffer,reproLock,jobs)
    reproThr.start()
    iBunch += 1

_memoryCheck("end")

_logger.debug("===================== end =====================")