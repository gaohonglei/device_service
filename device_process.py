#!/usr/bin/env python
# -*- coding=utf-8 -*-

import threading, Queue, sys, traceback, time, threadpool, re, subprocess, logging, os
import vistek_psia, vistek_hikvision, vistek_onvif, copy
import multiprocessing
#process_count = 0

file_name = "{0}-{1}.log".format(__name__, os.getpid())
file_path = os.path.join("log", str(os.getpid()))
try:
    if not os.path.exists(file_path):
        os.makedirs(file_path)
except:
    traceback.print_exc()
log_file = os.path.join(file_path, file_name)
log_level = logging.DEBUG
#log_level = logging.INFO

logger = logging.getLogger(file_name)
handler = logging.handlers.TimedRotatingFileHandler(log_file, when="H", interval=5,backupCount=1)
formatter = logging.Formatter(
    "[%(asctime)s] [%(levelname)s] [%(name)s] [%(filename)s:%(funcName)s:%(lineno)s]  %(message)s")

handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(log_level)
class Worker(threading.Thread):
    worker_count = 0
    def __init__( self, timeout = 0, **kwds):
        threading.Thread.__init__( self, **kwds )
        self.id = Worker.worker_count
        self._id = str(threading.currentThread())
        Worker.worker_count += 1
        self.setDaemon( True )
        self.workQueue = Queue.Queue()
        self.resultQueue = Queue.Queue()
        self.timeout = timeout
        self._is_stoped = False
        self.start()

    def run( self ):
        ''' the get-some-work, do-some-work main loop of worker threads '''
        while not self._is_stoped:
            try:
                callable, args, kwds = self.workQueue.get(timeout=self.timeout)
                res = callable(*args, **kwds)
                if res:
                    self.resultQueue.put( res )
                print "worker[%2d]: %s" % (self.id, str(res) )
            except Queue.Empty:
                break
            except :
                print 'worker[%2d]' % self.id, sys.exc_info()[:2]

    def stop(self):
        self._is_stoped = True

    def result_queue_size(self):
        return self.resultQueue.qsize()

    def result_queue_empty(self):
        return self.resultQueue.empty()

    def add_task(self, callable, *args, **kwds):
        self.workQueue.put((callable, args, kwds))

    def get_result(self, *args, **kwds):
        return self.resultQueue.get(*args, **kwds)

def is_device_on_line(ip):
    ping_cmd = "ping -n 2 {0}".format(str(ip))
    ret = os.system(ping_cmd)
    if ret:
        return False
    else:
        logger.info("device is on line but can't find process, device:{0}".format(str(ip)))
        return True
    # ping_cmd = "ping -c 2 {0}".format(str(device.IP))
    # p = subprocess.Popen([ping_cmd],
    #                     stdin = subprocess.PIPE,
    #                     stdout = subprocess.PIPE,
    #                     stderr = subprocess.PIPE,
    #                     shell = True)
    # out = p.stdout.read()
    # regex = re.compile("time=\d*", re.IGNORECASE | re.MULTILINE)
    # if len(regex.findall(out)) > 0:
    #     logger.info("device is on line but can't find process, device:", device)
    #     return True
    # else:
    #     return False

def is_device_can_process(device):
    if str(device.reserved) == "Unset":
        device.reserved = None
    if 0 == int(device.ProtocolFlag):
        return False
    else:
        return True

class DeviceOnLineThread(threading.Thread):
    def __init__(self):
        threading.Thread.__init__(self)
        self._device_map = {}#ip device_lists
        self._status_lock = threading.Lock()
        self._device_status = {}#ip status
    def push_device(self, device):
        if device.IP not in self._device_map:
            self._device_map[device.IP] = []
            self._device_map[device.IP].append(device)
            self._device_status[device.IP] = False
        else:
            dev_list = self._device_map.get(device.IP)
            if device not in dev_list:
                dev_list.append(device)

    def online_device_lists(self):
        device_list = []
        for ip, status in self._device_status.items():
            if status:
                device_list.extend(self._device_map[ip])
        return device_list

    def offline_device_lists(self):
        device_list = []
        for ip, status in self._device_status.items():
            if not status:
                device_list.extend(self._device_map[ip])
        return device_list

    def run(self):
        for ip, device_list in self._device_map.items():
            if is_device_on_line(ip):
                self._device_status[ip] = True
            else:
                self._device_status[ip] = False


can_proc_queue = Queue.Queue()
def get_can_proc_device_queue():
    global can_proc_queue
    return can_proc_queue

# un_proc_queue = Queue.Queue()
# def get_un_proc_device_queue():
#     global un_proc_queue
#     return un_proc_queue

def try_device_process(device, update_queue = None,Lock=None,success_list=[],failed_list=[]):
    """
    the device can use the current package to process.
    :device
    :return tuple (is_can_process, update device info)
    """

    logger.info("try process start id:{0} ip:{1} pid:{2} threadid:{3}".format(device.DeviceID\
                                                                              , device.IP\
                                                                              , os.getpid()\
                                                                              , threading.currentThread().ident))
    can_proc_device_list = []
    out_data = vistek_hikvision.try_process_device(device)
    # dev_queue = get_can_proc_device_queue()
    if out_data and out_data[1]:
        can_proc_device_list.append(device)
        # dev_queue.put(device)
        if update_queue is not None:
            if device.Status is None or device.Status == 1:
                device.Status = 0
            #logger.info("put update_queue-{0}".format(device))
            update_queue.put(device)
        if 0 < len(can_proc_device_list):
            logger.info("try process success hikvision device  id:{0} ip:{1} pid:{2} threadid:{3}"\
                        .format(device.DeviceID, device.IP, os.getpid(), threading.currentThread().ident))
            Lock.acquire()
            success_list.add(device.DeviceID)
            Lock.release()
            return can_proc_device_list
    out_data = vistek_psia.try_process_device(device)

    logger.debug("psia out:{0}".format(device))
    if out_data and out_data[1]:
        device.ProtocolFlag = out_data[2]
        can_proc_device_list.append(device)
        if update_queue is not None:
            if device.Status is None or device.Status == 1:
                device.Status = 0
            update_queue.put(device)
        # dev_queue.put(device)
        if 0 < len(can_proc_device_list):
            logger.info("try process success psia device id:{0} ip:{1} pid:{2} threadid:{3}"\
                        .format( device.DeviceID, device.IP, os.getpid(), threading.currentThread().ident))
            Lock.acquire()
            success_list.add(device.DeviceID)
            Lock.release()
            return can_proc_device_list
    # if un_proc_queue is not None:
    #     un_proc_queue.put(device)
    logger.warning("try process failed, device id:{0} ip:{1} pid:{2} threadid:{3}".format(device.DeviceID \
                                                                                              , device.IP \
                                                                                              , os.getpid() \
                                                                                              , threading.currentThread().ident))
    Lock.acquire()
    failed_list.add(device.DeviceID)
    Lock.release()
    return None#内存增长
    out_data = vistek_onvif.try_process_device(device)
    logger.debug("onvif out:{0}".format(out_data))
    if out_data and out_data[1]:
        device.ProtocolFlag = out_data[2]
        can_proc_device_list.append(device)
        if update_queue is not None:
            update_queue.put(device)
        # dev_queue.put(device)
        if 0 < len(can_proc_device_list):
            logger.info("try process success onvif device id:{0} ip:{1} pid:{2} threadid:{3}" \
                        .format( device.DeviceID, device.IP, os.getpid(), threading.currentThread().ident))
            return can_proc_device_list
    else:
        # if un_proc_queue is not None:
        #     un_proc_queue.put(device)
        logger.warning("try process failed, device id:{0} ip:{1} pid:{2} threadid:{3}".format(device.DeviceID\
                                                                                              , device.IP\
                                                                                              , os.getpid()\
                                                                                              , threading.currentThread().ident))
        return None


    ##################################################################
    #global process_count
    # try:
    #     print("time:{0} device_process begin thrd:{1}".format(time.asctime(time.localtime(time.time())), threading.currentThread()))
    #     do_proc_device_worker = Worker(timeout=10)
    #     do_proc_device_worker.add_task(vistek_onvif.try_process_device, device)
    #     do_proc_device_worker.add_task(vistek_psia.try_process_device, device)
    #     do_proc_device_worker.add_task(vistek_hikvision.try_process_device, device)
    #     if not do_proc_device_worker.isAlive():
    #          do_proc_device_worker.start()
    #     do_proc_device_worker.join()
    #     #worker_pool = threadpool.ThreadPool(3)
    #     #worker_req_lists = []
    #     #worker_req_lists.extend(threadpool.makeRequests(vistek_hikvision.try_device_process, [((device,), {})]))
    #     #worker_req_lists.extend(threadpool.makeRequests(vistek_onvif.try_process_device, [((device,), {})]))
    #     #worker_req_lists.extend(threadpool.makeRequests(vistek_psia.try_process_device, [((device,), {})]))
    #     #threadpool.WorkerThread()
    #     #for req in worker_req_lists:
    #     #    worker_pool.putRequest(req)
    #     #worker_pool.wait()
    #     print("time:{0} device_process success, thrd:{1} result count:{2}".format(time.asctime(time.localtime(time.time())), threading.currentThread(), do_proc_device_worker.result_queue_size()))
    #     #return can_proc_device_list
    # except:
    #     traceback.print_exc()
    # finally:
    #     do_proc_device_worker.stop()
    #     do_proc_device_worker.join(timeout=10)
    #
    # while not do_proc_device_worker.result_queue_empty():
    #     out_data = do_proc_device_worker.get_result()
    #     if out_data[1]:
    #         break

