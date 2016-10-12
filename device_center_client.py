#!/usr/bin/env python
# coding=utf-8


import copy, logging, logging.handlers, os, Ice, sys, threading, threadpool, time, traceback, uuid
import Vistek.Data as v_data
import Vistek.Device as v_device
import vistek_util.DeviceCallbackI as DeviceCallbackI
import eventlet
import multiprocessing
import gc
import objgraph

eventlet.monkey_patch(socket=True)
try:
    import Queue
except:
    import queue as Queue
try:
    import device_process
except:
    from . import device_process

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

#logger.disabled = True
formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] [%(name)s] [%(filename)s:%(funcName)s:%(lineno)s]  [%(message)s]")

handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(log_level)

# can_proc_device_queue = Queue.Queue()
# def get_can_proc_dev_queue():
#     global can_proc_device_queue
#     return can_proc_device_queue

whole_device_list = {}
def get_whole_device_list():
    global whole_device_list
    return whole_device_list

# try_proc_dev_queue = Queue.Queue()
# update_dev_queue = Queue.Queue()
try_proc_dev_queue = multiprocessing.Queue()#DmDevice:ProtocolFlag=0
update_proc_dev_queue = multiprocessing.Queue()#try_proc_dev_queue队列中设备补全信息后需要推送给上层服务的设备
update_dev_queue = Queue.Queue()#DmDevice:ProtocolFlag！=0的设备

class SessionKeepLiveThread(threading.Thread):
    def __init__(self,timeout, session, client=None):
        threading.Thread.__init__(self)
        self._session, self._timeout, self._terminated, self._cond = session, timeout, False, threading.Condition()
        self._client = None
        if client is not None:
            self._client = client

    def _reconnect(self):
        try:
            if self._client is not None:
                center_session = self._client.proxy.Register(self._client._register_info.id,self._client._register_info.type)
                self._client.session = center_session
                self._session = center_session
        except:
            traceback.print_exc()
    def run(self):
        self._cond.acquire()
        try:
            while not self._terminated:
                self._cond.wait(self._timeout)
                if not self._terminated:
                    try:
                        #t1=time.time()
                        self._session.KeepAlive()
                    #except Ice.LocalException as ex:
                    except Ice.ObjectNotExistException:#服务端删除session 重新获取session
                        traceback.print_exc()
                        self._reconnect()
                        continue
                    except:
                        continue
                        #self._reconnect(self._client)
        finally:
            self._cond.release()

    def terminate(self):
        self._cond.acquire()
        try:
            self._terminated = True
            self._cond.notify()
        finally:
            self._cond.release()

class CallBackKeepLiveThread(threading.Thread):
    def __init__(self, proxy, conn_str, client, communicator):
        threading.Thread.__init__(self)
        self._proxy = proxy
        self._conn_str = conn_str
        self._cond = threading.Condition()
        self._terminated = False
        self._communicator = communicator
        self._client = client
        self._timeout = 5# s

    def _reconnect(self):
        try:
            self._client._callback_proxy.ice_getConnection().setAdapter(self._client._callback_adapter)
            self._client._callback_proxy.SubscribeCallback(self._client._callback_ident)
        except:
                traceback.print_exc()
    def run(self):
        self._cond.acquire()
        try:
            while not self._terminated:
                self._cond.wait(self._timeout)
                if not self._terminated:
                    try:
                        self._proxy.ice_ping()
                    # except Ice.LocalException as ex:
                    except:
                        continue
                        #self._reconnect()
        except:
            self._cond.release()

    def terminate(self):
        self._cond.acquire()
        try:
            self._terminated = True
            self._cond.notify()
        finally:
            self._cond.release()

def enum(**enums):
    return type('Enum', (), enums)

def handle_request_result(request, result):
    pass
    # can_proc_device_queue = device_process.can_proc_queue
    # if result is not None and 0 < len(result):
        #logger.debug("request:{0} result:{1}".format(request, result))
        # can_proc_device_queue.put(result)

CLIENTTYPE = enum (CLIENTNONE=-1, CLIENTNOMAL=0, CLIENTFIND=1)

VProtocolType = enum(EMPTY=0, SDK=1, ONVIF=2, PSIA=4,  GB28181=8, HDCCTV=16)
DEVICE_MANU_TYPE = enum (HIKVISION="hikvision", DAHUA="dahua", PSIA="psia", ONVIF="onvif")

def proc_device(proc_queue, update_proc_queue):
    pool_size=32
    worker_pool = threadpool.ThreadPool(pool_size)
    un_proc_device_list = {}# deviceid try_counts
    device_set=set()
    Lock=threading.Lock()
    success_register_list=set()
    failed_register_list=set()
    #un_proc_queue = device_process.get_un_proc_device_queue()
    un_proc_queue = Queue.Queue()
    # LOCK=threading.Lock()    # count=[0]
    while True:
        try:
            if proc_queue.empty() and un_proc_queue.empty():
                time.sleep(0.01)
                # gc.collect()
                #objgraph.show_growth()
                continue
            process_dev_dict = {}
            i=0
            try:
                while(True):
                    #记录注册失败的设备，下次还会从上层服务中获取，那么将其加入到线程池中,
                    #暂时将注册成功的设备，如果下次继续取到，则也将其加入到线程池中，等到将注册成功的设备推送到上层服务后，下次
                    #就不会再取到了。这样做的原因是如果从客户端删除了注册成功的设备，然后又接着新添加这个设备，也能够让其处理。
                    logger.info("length of proc_queue:{0}".format(proc_queue.qsize()))
                    dev_dict=proc_queue.get(block=False)
                    if dev_dict is not None:
                        t1=time.time()
                        process_dev_dict.update(dev_dict)
                        Lock.acquire()
                        for k,v in dev_dict.items():
                            if k in success_register_list:
                                success_register_list.remove(k)
                                continue
                            elif k in failed_register_list:
                                failed_register_list.remove(k)
                                continue
                            elif k in device_set:
                                process_dev_dict.pop(k)
                        Lock.release()
                        t2=time.time()
                        logger.info("time consume is :{0}".format(str(t2 - t1)))
            except Queue.Empty:
                logger.info("length of threading pool result:{0}".format(worker_pool._requests_queue.qsize()))
                worker_pool.poll()
            finally:
                logger.info("length of process_dev_list:{0}".format(len(process_dev_dict)))
                for k,device in process_dev_dict.items():
                    logger.info("device:{0}".format(device))
                if 0 < len(process_dev_dict):
                    device_set.update(process_dev_dict.keys())
                    work_req = []
                    t1 = time.time()
                    for deviceID,device in process_dev_dict.items():
                        work_req.extend(threadpool.makeRequests(device_process.try_device_process \
                                                                , [((device, update_proc_queue,Lock,success_register_list,failed_register_list), {})] \
                                                                , handle_request_result))
                    if 0 < len(work_req):
                        map(worker_pool.putRequest, work_req)
                        del work_req
                        logger.info("length of threading pool result:{0}".format(worker_pool._requests_queue.qsize()))
                        worker_pool.poll()
                    t2 = time.time()
                    logger.info("time consume is :{0}".format(str(t2 - t1)))
                gc.collect()
                time.sleep(0.001)
        except:
            traceback.print_exc()
            continue

            #
            #
            # while not proc_queue.empty():
            #     logger.info("length of proc_queue:{0}".format(proc_queue.qsize()))
            #     dev_list = proc_queue.get()
            #     logger.info("length of dev_list:{0}".format(len(dev_list)))
            #     if 0 < len(dev_list):
            #         process_dev_list.extend(dev_list)
            #         logger.info("length of process_dev_List_upper:{0}".format(len(dev_list)))
            #     else:
            #         raise "device list empty"
            #     i=i+1
            #     logger.info("i:{0}".format(i))
            # logger.info("length of proc_queue:{0}".format(proc_queue.qsize()))
            # logger.info("length of empty:{0}".format(proc_queue.empty()))
            # if not un_proc_queue.empty():
            #     logger.info("un proc queue size:{0}".format(un_proc_queue.qsize()))
            # while not un_proc_queue.empty():
            #     device = un_proc_queue.get()
            #     #try 2 times but faile don't try again.
            #     if device.DeviceID in un_proc_device_list and un_proc_device_list[device.DeviceID]==2:
            #         un_proc_device_list.pop(device.DeviceID)
            #         continue
            #     if device.DeviceID not in un_proc_device_list:
            #         un_proc_device_list[device.DeviceID] = 1
            #     else:
            #         un_proc_device_list[device.DeviceID] +=1
            #     process_dev_list.append(device)
            # if 0 < len(process_dev_list):
            #     print("start, pid:{0} process_list:{1}".format(os.getpid(), process_dev_list))
            # if 0 < len(process_dev_list):
            #     print("process start count:{0}.".format(len(process_dev_list)))
                # logger.info("process start count:{0}.".format(len(process_dev_list)))

add_mutex = threading.Lock()
add_device_list = {}

class RegisterInfo:
    def __init__(self):
        self.id=None
        self.type=0
        self.ip="127.0.0.1"
        self.port=0
class DeviceCenterClient():
    def __init_ice_config(self, **properties):
        """
        : init ice base configs.
        :return communicator
        """
        init_data = Ice.InitializationData()
        center_pros = Ice.createProperties(sys.argv)
        center_pros.setProperty("Ice.MessageSizeMax", "5120")
        center_pros.setProperty("Ice.RetryIntervals", "0 100 500 1000")
        center_pros.setProperty("Ice.ThreadPool.Client.Size", "8")
        #center_pros.setProperty("Ice.Default.InvocationTimeout", "3000")
        #[center_pros.setProperty(key, item) for key, item in properties.items()]
        for key, item in properties.items():
            center_pros.setProperty(key, item)
        init_data.properties = center_pros
        self._communicator = Ice.initialize(sys.argv, init_data)
        return self._communicator

    def __init__(self, proxy_value, client_type=None):
        """

        :param proxy_value: string connect string.
        :param client_type: rtCheck|rtNormal
        """
        global try_proc_dev_queue#flag
        global update_proc_dev_queue
        self.__init_ice_config()
        self._call_back = None
        self._callback_proxy = None

        self._proxy_value = proxy_value#client need to connect server string.device_dispatch_service:tcp -h 172.16.0.80 -p 54321
        self._conn = None  # self._communicator.stringToProxy(self._proxy_value)
        self._proxy = None #v_device.DeviceDispatchServiceV1Prx.checkedCast(self._conn)
        self._center_session=None #self._proxy.Register(self._register_info)
        self._data_sender = None  # ghl添加DeviceDispatchServiceI中的_data_callback函数

        self._id = uuid.uuid4()
        self._whole_devices_list = {}#上次获取到的设备列表
        self._whole_devices_list_mutex = threading.Lock()
        self._unprocess_device_lists = {}
        self._device_count = {}#每次从session 获取到的设备列表中psia onvif sdk 分别对应的设备数量
        self._add_device_queue = Queue.Queue()#增加的设备
        self._del_device_queue = Queue.Queue()#删除的设备
        self._alter_device_queue = Queue.Queue()#改变状态的设备
        self._update_device_queue = Queue.Queue()
        self._dismissed = threading.Event()

        if not client_type:
            # client_type = v_data.RegisterType.rtNormal
            client_type = v_device.RegisterType.rtNormal
        # self._register_info, self._register_info.id, self._register_info.type = v_data.RegisterInfo(), \
         #                                                                        str(self._id), client_type
        '''
        class RegisterInfo
		{
			string id;
			RegisterType type = rtNormal;
			string ip = "127.0.0.1";
			int port = 0;
		};
        '''
        self._register_info, self._register_info.id, self._register_info.type = RegisterInfo(), \
                                                                                str(self._id), client_type
        self._connect()


        self._keep_live_thrd = None
        self._get_device_lists_thrd = threading.Thread(target=DeviceCenterClient._do_main_loop\
                                                       , name="get_device_lists", args=(self,))
        self._pre_check_device_thrd = threading.Thread(target=DeviceCenterClient._do_pre_check_device\
                                                       , name="pre_check_device", args=(self, ))
        # self._check_device_thrd = threading.Thread(target=DeviceCenterClient._do_try_process_device, name="check device", args=(self, ))
        # self._check_device_thrd = threading.Thread(target=DeviceCenterClient._do_try_process_device_by_eventlet\
        #                                            , name="check device by evenlet", args=(self, ))
        self._update_device_thrd = threading.Thread(target=DeviceCenterClient._do_update_device\
                                                    , name="update_device", args=(self, ))

        self._proc_dev_pro_list = []
        # print("start queue id:{0}".format(id(try_proc_dev_queue)))
        proc_count = 1
        for index in xrange(proc_count):
            proc_name = "preproc_device_{0}".format(index)
            self._proc_dev_pro_list.append(multiprocessing.Process(target=proc_device, name=proc_name, args=(try_proc_dev_queue, update_proc_dev_queue)))
        # self._proc_dev_pro_list.append(multiprocessing.Process(target=proc_device, name="test_proc-2", args=(try_proc_dev_queue, update_proc_dev_queue)))

    @property
    def device_counts(self):
        """

        :return: device_count map (id, counts)
        """
        return self._device_count

    @property
    def session(self):
        return self._center_session

    @session.setter
    def session(self, session):
        self._center_session = session
    @property
    def conn(self):
        return self._conn

    @property
    def proxy(self):
        return self._proxy

    @proxy.setter
    def proxy(self, proxy):
        if proxy != self._proxy:
            self._proxy = proxy
    @property
    def addDeviceQueue(self):
        return self._add_device_queue

    @property
    def delDeviceQueue(self):
        return self._del_device_queue

    @property
    def alterDeviceQueue(self):
        return self._del_device_queue

    def subscribe_data_callback(self, func):
        self._data_sender = func

    def data_callback(self, *args, **kwargs):
        if self._data_sender is not None:
            self._data_sender(*args, **kwargs)

    def device_change_callback(self, *args, **kwargs):
        global try_proc_dev_queue
        global update_dev_queue
        global add_device_list
        device = args[0]
        data_change_type = args[1]
        if isinstance(device, v_data.DmDeviceVideoChannel) and v_data.DataChangeTypes.DataAdded == data_change_type:
            channel = device
            logger.info("device_change_callback---{0}".format(device))
            if channel.DeviceID in add_device_list:
                add_device = add_device_list.get(channel.DeviceID)
                # print("come here1 dev:{0} list type:{1}".format(add_device, type(add_device.ChannelList)))
                if add_device.ChannelList is None:
                    add_device.ChannelList = []
                    # print("come here2")
                    add_device.ChannelList.append(channel)
                else:
                    # print("come here3")
                    add_device.ChannelList.append(channel)
                update_dev_queue.put([add_device])
            return

        try:
            if data_change_type == v_data.DataChangeTypes.DataAdded:
                # with self._whole_devices_list_mutex:
                #     if device.DeviceID not in self._whole_devices_list:
                #         self._whole_devices_list.update({device.DeviceID:device})
                    # else:
                    #     return
                logger.info("device_change_callback---{0}".format(device))
                if device.ProtocolFlag == 0 or device.Manufacture is None or 1 > len(device.Manufacture):
                    if str(device.reserved) == "Unset":
                        device.reserved = None
                    # try_proc_dev_queue.put([(device.DeviceID, device.IP, device.Port, device.Username, device.Password),])
                    try_proc_dev_queue.put({device:device.DeviceID})
                else:
                    if device.DeviceID not in add_device_list:
                        add_device_list[device.DeviceID] = device
                    # update_dev_queue.put([device])
                # print("come callback queue id:{0} device:{1}".format(id(try_proc_dev_queue), device))
                logger.debug("device add begin device:{0} type:{1}".format(device, data_change_type))
                logger.debug("device add begin device:{0}-{1} type:{2} try count:{3} update count:{4}".format(\
                        device.DeviceID\
                        , device.IP\
                        , data_change_type\
                        , try_proc_dev_queue.qsize()\
                        , update_dev_queue.qsize()))
                # dev_list = device_process.try_device_process(device)
                # if dev_list is not None and 0 < len(dev_list):
                #     logger.info("device add device success:{0}".format(device))
                #     self.session.UpdateDeviceList(dev_list)
            elif data_change_type == v_data.DataChangeTypes.DataRemoved:
                logger.debug("device del begin device:{0} type:{1}".format(device, data_change_type))
                self._whole_devices_list.pop(device.DeviceID)
                # self._del_device_queue.put({device.DeviceID:device})
            else:
                logger.debug("device change device:{0} type:{1}".format(device, data_change_type))
        except:
            print("come exceptions")
            traceback.print_exc()

    def start(self):
        """
        start a center client according the type.
        normal:start get the devices that getted by servers thread.
        check: get the devices and try proccess devices.
        :return None
        """
        if self._register_info.type == v_device.RegisterType.rtNormal:
            if self._get_device_lists_thrd:
                self._get_device_lists_thrd.start()
        elif self._register_info.type == v_device.RegisterType.rtCheck:
             #if self._check_device_thrd:
                 #self._check_device_thrd.start()
             if self._pre_check_device_thrd:
                 self._pre_check_device_thrd.start()
             if 0 < len(self._proc_dev_pro_list):
                 for proc in self._proc_dev_pro_list:
                     proc.daemon = True
                     proc.start()
             if self._update_device_thrd:
                 self._update_device_thrd.start()
        else:
             logger.error("client type error")

    def stop(self):
        """
        stop the thread.
        :return
        """
        print("stop")
        self._dismissed.set()
        if self._get_device_lists_thrd and self._get_device_lists_thrd.is_alive():
            self._get_device_lists_thrd.join()
        del self._get_device_lists_thrd
        if self._check_device_thrd and self._check_device_thrd.is_alive():
            self._check_device_thrd.join()
        del self._check_device_thrd
        if self._keep_live_thrd and self._keep_live_thrd.is_alive():
            self._keep_live_thrd.terminate()
        del self._keep_live_thrd
        if self._update_device_thrd and self._update_device_thrd.is_alive():
            self._update_device_thrd.terminate()
        del self._update_device_thrd
        #self._conn.destory()
        self._communicator.destroy()

    def _add_devices(self, cur_device_lists, new_device_lists):
        """
        compare the devices to find the new deivces to add.
        new_device_lists必须是ProtocolFlag！=0也就是协议类型确定
        :param cur_device_lists: dict
        :param new_device_lists: dict
        :return: dict need to add new device dict.
        """
        return dict((item, new_device_lists.get(item))for item in list(set(new_device_lists.keys()) - set(cur_device_lists.keys())))
        # add_devices = {}
        # for id, device in new_device_lists.items():
        #     if id not in cur_device_lists:
        #         add_devices[device.DeviceID] = device
        # return add_devices

    def _del_devices(self, cur_device_lists, new_device_lists):
        """

        :param cur_device_lists: dict
        :param new_device_lists: dict
        :return: dict need to del device dict.
        """
        return dict((item, cur_device_lists.get(item)) for item in list(set(cur_device_lists.keys()) - set(new_device_lists.keys())))
        # del_devices = {}
        # for id, device in cur_device_lists.items():
        #     if id not in new_device_lists:
        #         del_devices[device.DeviceID] = device
        # return del_devices

    def _alter_devices(self, cur_device_lists, new_device_lists):
        """
        need to find the alter devices according rules.
        :param cur_device_lists: dict
        :param new_device_lists: dict
        :return dict need to alter device dict.
        """
        alter_devices = {}
        return alter_devices

    def _set_callback(self):
        self._callback_proxy = v_device.DeviceDispatchServiceV1Prx.checkedCast(self._conn)
        self._keep_callback_thread = CallBackKeepLiveThread(self._callback_proxy, self._conn, self, self._communicator)
        self._keep_callback_thread.start()
        self._callback_adapter = self._communicator.createObjectAdapter("")
        self._callback_ident = Ice.Identity()
        self._callback_ident.name = Ice.generateUUID()
        self._callback_ident.category = ""
        self._call_back = DeviceCallbackI.deviceCallBackI()
        self._callback_adapter.add(self._call_back, self._callback_ident)
        self._callback_adapter.activate()
        self._callback_proxy.ice_getConnection().setAdapter(self._callback_adapter)
        self._callback_proxy.SubscribeCallback(self._callback_ident)
        if self._call_back is not None:
            # func = getattr(self, "data_callback")
            # if func is not None:
            #     self._call_back.subscribe_ptz_cmd(func)
            device_change_func = getattr(self, "device_change_callback")
            if device_change_func is not None:
                self._call_back.subscribe_device_change(device_change_func)

    def _connect(self,timeout=5):
        """
        connect to server.
        :param timeout: int keep live interval
        :return a proxy to call the server service.
        """
        while True:
            try:
                self._conn = self._communicator.stringToProxy(self._proxy_value)
                self._proxy = v_device.DeviceDispatchServiceV1Prx.checkedCast(self._conn)
                if not self._proxy:
                    continue
                self._center_session = self._proxy.Register(str(self._id), self._register_info.type)
                if not self._center_session:
                    continue
                self._center_session.ice_invocationTimeout(2500)
                self._keep_live_thrd = SessionKeepLiveThread(timeout, self._center_session, self)
                self._keep_live_thrd.start()
                if v_device.RegisterType.rtCheck == self._register_info.type:
                    self._set_callback()
                return True
            except Exception:
                logger.error("connect failed")
                traceback.print_exc()
                continue

    def _do_get_update_device(self):
        pass

    def _do_pre_check_device(self):
        global update_dev_queue
        global try_proc_dev_queue

        new_device_lists = {}
        device_lists = None
        while True:
            logger.info("length of _whole_devices_list:{0}".format(len(self._whole_devices_list)))
            try:
                if self._dismissed.is_set() or not self.session:
                    break
                device_lists = self.session.GetDeviceList()
                logger.debug("Get Device Lists, count:{0}".format(len(device_lists)))
                get_devices_fail_interval = 5# s
                if 1 > len(device_lists):
                    time.sleep(get_devices_fail_interval)
                    continue

                try_dev_list = []
                update_dev_list = []
                # with self._whole_devices_list_mutex:
                cur_new_device = [device for device in device_lists if device.DeviceID not in self._whole_devices_list]
                cur_old_device = [device for device in device_lists if device.DeviceID in self._whole_devices_list]
                logger.debug("length of cur_new_device:{0}".format(len(cur_new_device)))
                #针对新添加的设备，补全设备信息
                for device in cur_new_device:
                    if device_process.is_device_can_process(device):
                        update_dev_list.append(device)
                    else:
                        try_dev_list.append(device)
                #设备服务重启后，将所有补全过得设备重新推一次
                for device in cur_old_device:
                    if device_process.is_device_can_process(device):
                        update_dev_list.append(device)
                logger.debug("Length of update_dev_list:{0}".format(len(update_dev_list)))
                logger.debug("Length of try_dev_list:{0}".format(len(try_dev_list)))
                cur_device_dict = {device.DeviceID:device for device in update_dev_list}
                if 0 < len(cur_device_dict):
                    self._whole_devices_list.update(cur_device_dict)
                if 0 < len(try_dev_list):
                    try_device_dict={device.DeviceID:device for device in try_dev_list}
                    try_proc_dev_queue.put(try_device_dict)
                if 0 < len(update_dev_list):
                    # self._update_device_queue.put(update_dev_list)
                    update_dev_queue.put(update_dev_list)
                new_device_lists.clear()
                cur_device_dict.clear()
                time.sleep(5)
            except :
                traceback.print_exc()
                continue
        logger.debug("center session:{0}".format(self.session) )
        self._communicator.waitForShutdown()

    def _do_try_process_device(self, pool_size=100):
        worker_pool = threadpool.ThreadPool(pool_size)
        while True:
            try:
                process_dev_list = []
                while not try_proc_dev_queue.empty():
                    dev_list = try_proc_dev_queue.get()
                    process_dev_list.extend(dev_list)
                un_proc_queue = device_process.get_un_proc_device_queue()
                while not un_proc_queue.empty():
                    device = un_proc_queue.get()
                    process_dev_list.append(device)
                if 0 < len(process_dev_list):
                    logger.info("process start count:{0}.".format(len(process_dev_list)))
                work_req = []
                for device in process_dev_list:
                    work_req.extend(threadpool.makeRequests(device_process.try_device_process, [((device,), {})], handle_request_result))
                if 0 < len(work_req):
                    map(worker_pool.putRequest, work_req)
                    worker_pool.poll()
                time.sleep(0.001)
            except:
                continue

    def _do_try_process_device_by_eventlet(self):
        task_pool = eventlet.greenpool.GreenPool()
        global try_proc_dev_queue
        while 1:
            # if self._dismissed.is_set() or self.session is None:
            #     break
            process_dev_list = []
            while not try_proc_dev_queue.empty():
                dev_list = try_proc_dev_queue.get()
                process_dev_list.extend(dev_list)
            if 0 < len(process_dev_list):
                logger.info("process start count:{0}.".format(len(process_dev_list)))
            waiters = []
            for device in process_dev_list:
                waiters.append(task_pool.spawn(device_process.try_device_process, device))
            if 0 < len(waiters):
                [waiter.wait() for waiter in waiters]
            if 0 < len(process_dev_list):
                logger.info("process finished count:{0}.".format(len(process_dev_list)))
            time.sleep(0.01)

    def _do_update_device(self):
        global update_dev_queue
        global update_proc_dev_queue
        update_count = 0
        while 1:
            # can_proc_device_queue = device_process.get_can_proc_device_queue()
            can_dev_list = []
            up_dev_list= []

            #从需要推送到上层的补全设备的队列中，获取设备，然后推动到上层服务，由于multiprocess 的队列中empty,qsize,等函数
            #不是可靠的，下面都是用了get的非阻塞方式
            try:
                while True:
                    device=update_proc_dev_queue.get(block=False)
                    can_dev_list.append(device)
            except Queue.Empty:
                if self.session is not None and 0 < len(can_dev_list):
                    logger.info("update device success,start pid:{0}".format(threading.currentThread().ident))
                    logger.info("device_detail:{0}".format(can_dev_list[0]))
                    try:
                        self.session.UpdateDeviceList(can_dev_list)
                        update_count += len(can_dev_list)
                        id_tuple = ["{0}:{1}".format(dev.DeviceID, dev.IP) for dev in can_dev_list]
                        id_str = ";".join(id_tuple)

                        logger.info("update device success,end len:{0} dev_lists:{1} whole count:{2}".format(\
                                    len(can_dev_list)\
                                    , id_str\
                                    , update_count))
                    except:#session可能导致的异常
                        #当网络出现异常时，UpdatedeviceList 会报出异常，到时从队列中取出的设备无法正常发送到dserver端，
                        #因此必须将已经取出的设备放回到队列中
                        for device in can_dev_list:
                            update_proc_dev_queue.put(device)

            #从需要推送到上层的补全设备的队列中，获取设备，然后推动到上层服务，由于multiprocess 的队列中empty,qsize,等函数
            #不是可靠的，下面都是用了get的非阻塞方式
            can_dev_list=[]
            try:
                while True:
                    device=update_dev_queue.get(block=False)
                    up_dev_list.extend(device)
            except Queue.Empty:
                if self.session is not None and 0 < len(up_dev_list):
                    try:
                        self.session.UpdateDeviceList(up_dev_list)
                        #update_count += len(up_dev_list)
                        #id_tuple = ["{0}:{1}".format(dev.DeviceID, dev.IP) for dev in up_dev_list]
                        #id_str = ";".join(id_tuple)
                        #logger.info("update device success by event,end len:{0} dev_lists:{1}".format(\
                        #        len(up_dev_list)\
                         #       , id_str\
                        #        , update_count))
                    except Exception,ex:
                        logger.info("Exception:{0}".format(ex))
                        #当网络出现异常时，UpdatedeviceList 会报出异常，到时从队列中取出的设备无法正常发送到dserver端，
                        #因此必须将已经取出的设备放回到队列中
                        for device in up_dev_list:
                            update_dev_queue.put(device)
            time.sleep(1)

    def __acount_device(self, device_lists):
        '''
        计数各种设备类型对应的设备数量
        :param device_lists:session getDeviceList()返回的全部设备列表
        :return: {device_type:counts}
        '''
        psia_counts = hik_counts = onvif_counts = 0
        for device in device_lists:
            if device.ProtocolFlag == VProtocolType.PSIA:
                psia_counts += 1
            elif device.ProtocolFlag == VProtocolType.SDK and str(device.Manufacture).lower() == "hikvision":
                hik_counts += 1
            elif device.ProtocolFlag == VProtocolType.ONVIF:
                onvif_counts += 1
        device_count = {}
        if 0 < hik_counts:
            device_count[DEVICE_MANU_TYPE.HIKVISION] = hik_counts
        if 0 < psia_counts:
            device_count[DEVICE_MANU_TYPE.PSIA] = psia_counts
        if 0 < onvif_counts:
            device_count[DEVICE_MANU_TYPE.ONVIF] = onvif_counts
        self._device_count.update(device_count)
        out_str = "center all-device counts:{0} hikvision:{1} onvif:{2} psia:{3}".format(len(device_lists), hik_counts, onvif_counts, psia_counts)
        logger.debug(out_str)
        return device_count

    def __sort_device_by_old(self, old_device_lists, new_device_lists):
        '''
        分类是新增加的设备还是删除的设备，或者修改的设备
        :param old_device_lists:
        :param new_device_lists:
        :return:
        '''
        add_list = self._add_devices(old_device_lists, new_device_lists)
        if 0 < len(add_list):
            self._add_device_queue.put(add_list)
            logger.debug("add lists dev:{0}".format(add_list))
        del_list = self._del_devices(old_device_lists, new_device_lists)
        if 0 < len(del_list):
            self._del_device_queue.put(del_list)
            logger.debug("del lists dev:{0}".format(add_list))
        alter_list = self._alter_devices(old_device_lists, new_device_lists)
        if 0 < len(alter_list):
            self._alter_device_queue.put(alter_list)
        out_str = "center all-device counts:{0} add:{1} del:{2} alter:{3}".format(len(new_device_lists)
                                                                                  , len(add_list), len(del_list)
                                                                                  , len(del_list))
        logger.debug(out_str)

    def _do_main_loop(self):
        '''
        每隔30s,从上层服务获取原始设备列表
        :return:
        '''
        new_device_lists = {}
        device_lists = None
        while True:
            try:
                if self._dismissed.is_set() or not self.session:
                    break
                device_lists = self.session.GetDeviceList()#从接口返回sequence<DmDevice> DmDeviceList;
                get_devices_fail_interval = 5# s
                if 1 > len(device_lists):
                    time.sleep(get_devices_fail_interval)
                    continue
                logger.debug("Get Device Lists, count:{0}".format(len(device_lists)))
                # if len(self._whole_devices_list) != len(device_lists):
                #     logger.debug("now device lists:{0}".format(device_lists))
                #else:#no device add, change, or del
                new_device_lists.update(dict((device.DeviceID,device) for device in device_lists if device.ProtocolFlag\
                                             != 0 and 0 < len(device.Manufacture)))
                if len(self._whole_devices_list) == len(device_lists):#error 错误 对象之间set无作用
                    if 1 > len(list(set(new_device_lists.keys()) - set(self._whole_devices_list.keys()))):
                        device_lists[:] = []
                        new_device_lists.clear()
                        time.sleep(2)#
                        continue
                #logger.debug("can proccess dev:{0}".format(new_device_lists))
                #for device in device_lists:
                #    new_device_lists[str(device.DeviceID)] = device
                self.__acount_device(device_lists)
                self.__sort_device_by_old(self._whole_devices_list, new_device_lists)
                logger.debug("Get Device addqueue:{0} del:{1} alter:{2}".format(self._add_device_queue.qsize(),
                                                                              self._del_device_queue.qsize(),
                                                                              self._alter_device_queue.qsize()))
                if 0 < len(new_device_lists):
                    #del self._whole_devices_list[:]
                    #self._whole_devices_list = copy.deepcopy(new_device_lists)
                    self._whole_devices_list.update(new_device_lists)
                    for item in (set(self._whole_devices_list.keys())-set(new_device_lists.keys())):
                        self._whole_devices_list.pop(item)
                new_device_lists.clear()
            except Exception as e:
                print("exception:", e)
                continue
        #finally:
            #if self._keep_live_thrd:
            #    self._keep_live_thrd.terminate()
            #    self._keep_live_thrd.join()
        logger.debug("center session:{0}".format(self.session) )
        self._communicator.waitForShutdown()

if __name__ == "__main__":
    #center_client = DeviceCenterClient("device_dispatch_service:tcp -h localhost -p 54300", v_device.RegisterType.rtNormal)
    center_client = DeviceCenterClient("device_dispatch_service:tcp -h localhost -p 54300", v_device.RegisterType.rtCheck)
    #center_client = DeviceCenterClient("device_dispatch_service:tcp -h 172.16.0.20 -p 54321", v_device.RegisterType.rtCheck)
    center_client.start()
    raw_input()
    center_client.stop()
    raw_input()

