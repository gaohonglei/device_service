#!/usr/bin/env python
# -*- coding=utf-8 -*-

import Ice, sys, uuid, threading, time, Queue, copy, re, logging.handlers, os, traceback, threadpool, functools
import Vistek.Device as v_device
import Vistek.Data as v_data
import logging, objgraph
import eventlet, gc
import MySQLdb, socket
import collections
v_device.
try:
    import xml.etree.cElementTree as ET
except:
    import xml.etree.ElementTree as ET

import vistek_hikvision, vistek_onvif, vistek_psia
import vistek_util.loggerTemplate as VLOGGER
import vistek_util.DeviceCallbackI as DeviceCallbackI
from vistek_util import DBHelper
from vistek_util import DBTypes
import sqlalchemy
import datetime
REGISTER_TABLE_NAME = 'deviceregisterinfo'
LOADINFO_TABLE_NAME = "serviceloadinginfo"
SERVER_INFO_TABLE_NAME = "deviceserverinfo"
try:
    import watch_types
except:
    from . import watch_types
def enum(**enums):
    return type('Enum', (), enums)

VManufacture = enum(HIKVISION="hikvision", DAHUA="dahua", VISTEK="vistek")
VProtocolType = enum(EMPTY=0, SDK=1, ONVIF=2, PSIA=4,  GB28181=8, HDCCTV=16)

report_data_path = os.path.join("report_data")
try:
    if not os.path.exists(report_data_path):
        os.makedirs(report_data_path)
except:
    traceback.print_exc()

file_name = "{0}-{1}.log".format(__name__, os.getpid())
file_path = os.path.join("log", str(os.getpid()))
try:
    if not os.path.exists(file_path):
        os.makedirs(file_path)
except:
    traceback.print_exc()
log_file = os.path.join(file_path, file_name)
log_level = logging.DEBUG
# log_level = logging.INFO

logger = logging.getLogger(file_name)
handler = logging.handlers.TimedRotatingFileHandler(log_file, when="H", interval=5,backupCount=1)
formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] [%(name)s] [%(filename)s:%(funcName)s:%(lineno)s]  [%(message)s]")

handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(log_level)
class SessionKeepLiveThread(threading.Thread):
    def __init__(self,timeout, session, client=None):
        threading.Thread.__init__(self)
        self._session = session
        self._timeout = timeout
        self._terminated = False
        self._client = client
        self._callback_client = None
        self._cond = threading.Condition()

    def _reconnect(self, client):
        try:
            if self._client is not None:
                center_session = self._client.proxy.Register(self._client._register_info)
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
                        #logger.debug("client time{0} send keeplive cmd".format(time.asctime(time.localtime(time.time()))))
                        self._session.KeepAlive()
                    except Ice.LocalException as ex:
                        self._reconnect(self._client)
                        # if self._client is not None:
                        #     proxy = v_device.DeviceDispatchServiceV1Prx.uncheckedCast(self._client.conn)
                        #     self._client.proxy = proxy
                        #     center_session = proxy.Register(self._client._register_info)
                        #     self._client.session = center_session
                        #self._terminated = True
        finally:
            self._cond.release()

    def terminate(self):
        self._cond.acquire()
        try:
            self._terminated = True
            self._cond.notify()
        finally:
            self._cond.release()

class RegisterInfo():
    def __init__(self, register_success_count, register_fail_count, register_success_list, register_fail_list):
        self._register_success_count = register_success_count
        self._register_fail_count = register_fail_count
        self._register_success = register_success_list
        self._register_fail = register_fail_list

class DispatchClient():
    def __init__(self, manu, proxy_value, db_config_file=None):
        self._manuc = manu#psia hikvision
        self._communicator = Ice.initialize(sys.argv)
        self._proxy = None#self._proxy = v_device.DeviceDispatchServiceV1Prx.checkedCast(self._conn)
        self._callback_proxy = None
        self._proxy_value = proxy_value#server中endporints_lists = self._adapter.getEndpoints()
        self._conn = self._communicator.stringToProxy(self._proxy_value)#proxy
        self._id = uuid.uuid4()
        self._dev_list_mutex = threading.Lock()
        self._whole_devices_list = {}#上一次getDeviceList 设备列表
        self._add_device_queue = Queue.Queue()
        self._del_device_queue = Queue.Queue()
        self._alter_device_queue = Queue.Queue()
        # if self._manuc == "psia":
        #     # warning
        #     # hikvison patch  online device:slower  offline device :faster
        #     eventlet.monkey_patch(socket=True)
        self._connect()
        if db_config_file is not None:
            db_config_content = self.__parse_db_file_config(db_config_file)
            db_para = self.parse_db_config_file(db_config_file)
            if db_config_content is not None and isinstance(db_config_content, tuple):
                if db_config_content[0] == 'mysql':
                    #self._db_helper = MySqlDbHelper(*db_config_content[1:])
                    self._db_storage=MysqlDB_init(db_para)
                    #self._get_service_status_info_thrd = threading.Thread(target=DispatchClient._doGetServiceStatusInfo, args=(self,))
                    self._pushStreamUriToMysql_thrd=threading.Thread(target=DispatchClient._doPushStreamUriToMysql,args=(self,))
                elif db_config_content[0] == 'sqlite':
                    self._db_helper = DBHelper.SQLLiteHelper(*db_config_content[1:])
                    self._get_service_status_info_thrd = threading.Thread(target=DispatchClient._doGetServiceStatusInfoIntoSqlite, args=(self,))
                    self._pushStreamUriToMysql_thrd=threading.Thread(target=DispatchClient._doPushStreamUriToMysql,args=(self,))
        self._get_device_lists_thrd = threading.Thread(target=DispatchClient._do_main_loop, name="get_device_lists", args=(self,))
        # self._register_thrd = threading.Thread(target=DispatchClient._do_register_device, name="register device", args=(self,))
        self._register_thrd = threading.Thread(target=DispatchClient._do_register_device_bythreadpool, name="register device", args=(self,))
        # self._register_thrd = threading.Thread(target=DispatchClient._do_register_device_byeventlet, name="register device", args=(self,))
        self._push_status_thrd = threading.Thread(target=DispatchClient._pushStatus, name="push_status_lists", args=(self, ))

        self._device_stream_uri_mutex = threading.Lock()
        self._stream_uri_queue = Queue.Queue()
        self._device_stream_uri_map = dict()
        self._failed_url_device=set()#ghl 添加 记录获取流地址失败的设备
        self.StreamUriQueue = Queue.Queue()  ##获取流地址线程回调函数，处理返回流地址，并且压入，v_device.DeviceStreamInfo()
        self.StreamUriQueueDB = Queue.Queue()  #单独一个线程用来 将流地址插入到数据库中，获取流地址线程回调函数，处理返回流地址，并且压入，v_device.DeviceStreamInfo()
        self.WholeDevListQueue = Queue.Queue()  # 每次从获取设备列表的session中设备放入到此全局队列中

        self._get_stream_uri_interval = 60
        self._push_stream_uri_interval = 60
        self._get_stream_uri_thrd = threading.Thread(target=DispatchClient._doGetStreamUri, args=(self,))
        # self._get_stream_uri_thrd = threading.Thread(target=DispatchClient._doGetStreamUriByEventlet, args=(self,))
        self._push_stream_uri_thrd = threading.Thread(target=DispatchClient._doPushStreamUri, args=(self,))
        # self._push_stream_uri_thrd = threading.Thread(target=DispatchClient._doPushStreamUriByEventlet, args=(self,))

    def __parse_db_file_config(self, db_file):
        if db_file is not None:
            if os.path.exists(db_file):
                db_tree = ET.ElementTree()
                db_tree.parse(db_file)
                root_node = db_tree.getroot()
                if root_node is not None:
                    db_type = root_node.get("dbtype")
                    if db_type == "mysql":
                        host, user, pwd, db = root_node.get("host")\
                            , root_node.get("user")\
                            , root_node.get("passwd")\
                            , root_node.get("db")
                        return (db_type, host, user, pwd, db)
                    elif db_type == "sqlite":
                        connect_str = root_node.get("connect_str")
                        return (db_type, connect_str)
                    else:
                        raise "db config file error!!!"
        return None

    def parse_db_config_file(self,db_file):
        if db_file is None or not os.path.exists(db_file):
            raise Exception, "Error:mysql config file is None or db_file is not exists"
        db = collections.namedtuple("parameters", "type host port user passwd db table")
        db_tree = ET.ElementTree()
        db_tree.parse(db_file)
        root_node = db_tree.getroot()
        if root_node is not None:
            db_type = root_node.get("dbtype")
            if db_type == "mysql":
                db_para = db(type=root_node.get("type")
                             , host=root_node.get("host")
                             , port=root_node.get("port")
                             , passwd=root_node.get("passwd")
                             , user=root_node.get("user")
                             , db=root_node.get("db")
                             , table=root_node.get("table"))
                return db_para
        else:
            raise Exception, "db config file error"
    def start(self):
        if self._get_device_lists_thrd:
            self._get_device_lists_thrd.start()
        if self._register_thrd:
            self._register_thrd.start()
        if self._push_status_thrd:
            self._push_status_thrd.start()
        if self._get_stream_uri_thrd:
            self._get_stream_uri_thrd.start()
        if self._push_stream_uri_thrd:
            self._push_stream_uri_thrd.start()
        if hasattr(self, "_get_service_status_info_thrd") and self._get_service_status_info_thrd:
            self._get_service_status_info_thrd.start()
        if self._pushStreamUriToMysql_thrd:
            self._pushStreamUriToMysql_thrd.start()

    def stop(self):
        if self._get_device_lists_thrd:
            self._get_device_lists_thrd.join()
        del self._get_device_lists_thrd
        if self._push_status_thrd.isAlive():
            self._push_status_thrd.join()
        if self._get_stream_uri_thrd:
            self._get_stream_uri_thrd.join()
        del self._get_stream_uri_thrd
        if self._push_stream_uri_thrd:
            self._push_stream_uri_thrd.join()
        del self._push_stream_uri_thrd
        if self._register_thrd:
            self._register_thrd.join()
        del self._register_thrd
        if self._get_service_status_info_thrd:
            self._get_service_status_info_thrd.join()
        del self._get_service_status_info_thrd
        self._conn.destory()

    @property
    def session(self):
        return self._center_session

    @property
    def addDeviceQueue(self):
        return self._add_device_queue

    @property
    def delDeviceQueue(self):
        return self._del_device_queue

    @property
    def alterDeviceQueue(self):
        return self._del_device_queue

    def data_callback(self, *args, **kwargs):
        msgs = list()
        for msg_arg in args:
            msgs.append(msg_arg.body)
        for msg in msgs:
           if self._manuc == "hikvision":
               vistek_hikvision.ptz_cmd(msg)
        #print("last:{0} last1:{1}".format(args, kwargs))

    def _add_devices(self, cur_device_lists, new_device_lists):
        return  dict((item, new_device_lists.get(item)) for item in list(set(new_device_lists.keys()) - set(cur_device_lists.keys())))
        # add_devices = dict()
        # for id, device in new_device_lists.items():
        #     if id not in cur_device_lists:
        #         add_devices[device.DeviceID] = device
        # return add_devices

    def _del_devices(self, cur_device_lists, new_device_lists):
        return dict((item, cur_device_lists.get(item)) for item in list(set(cur_device_lists.keys()) - set(new_device_lists.keys())))
        # del_devices = dict()
        # for id, device in cur_device_lists.items():
        #     if id not in new_device_lists:
        #         del_devices[device.DeviceID] = device
        # return del_devices

    def _register_device(self, device):
        register_uri = "http://{0}:{1}@{2}:{3}/device?func=register_device".format(str(device.Username), str(device.Password), str(device.IP), device.Port)
        if device.ProtocolFlag == VProtocolType.SDK:
            if device.Manufacture == VManufacture.HIKVISION:
                vistek_hikvision.request_cmd(device.DeviceID, register_uri, device.ChannelList)
        elif device.ProtocolFlag == VProtocolType.ONVIF:
            vistek_onvif.request_cmd(device.DeviceID, register_uri, "")
        elif device.ProtocolFlag == VProtocolType.PSIA:
            vistek_psia.request_cmd(device.DeviceID, register_uri,device.ChannelList)
        else:
            logger.error("client:{0} device:{1} can't proceess".format(self._manuc, device))

    def _unregister_device(self, device):
        unregister_uri = "http://{0}:{1}/device?func=unregister_device".format(str(device.IP), str(device.Port))
        if device.ProtocolFlag == VProtocolType.SDK:
            if device.Manufacture == VManufacture.HIKVISION:
                vistek_hikvision.request_cmd(device.DeviceID, unregister_uri, "")
        elif device.ProtocolFlag == VProtocolType.ONVIF:
            vistek_onvif.request_cmd(device.DeviceID, unregister_uri, "")
        elif device.ProtocolFlag == VProtocolType.PSIA:
            vistek_psia.request_cmd(device.DeviceID, unregister_uri, "")
        else:
            logger.error("client:{0} device:{1} can't proceess".format(self._manuc, device))

    def _alter_devices(self, cur_device_lists, new_device_lists):
        """
        need to compare according rules.
        """
        alter_devices = dict()
        return alter_devices

    def _pushStatus(self):
        global report_data_path
        if self._manuc == "hikvision":
            queue = vistek_hikvision.getStatusQueue()
        elif self._manuc == "psia":
            queue = vistek_psia.getStatusQueue()
        #elif self._manuc == "onvif":
            #queue = vistek_onvif
        #while self._push_status_thrd and self._push_status_thrd.isAlive():
        file_name = "devicestatus-{0}-{1}.xml".format(self._manuc, os.getpid())
        save_file = os.path.join(report_data_path, file_name)
        device_xml_node = ET.Element('device_status_list')
        etree = ET.ElementTree(device_xml_node)
        all_count = 0
        need_push_device_status_list = []
        service_id = "{0}:{1}".format(self._manuc, os.getpid())
        while 1:
            try:
            # if 0 < queue.qsize():
            #     logger.debug("{0} client QueueSize:{1} values:".format(self._manuc, queue.qsize()))
            #     all_count += queue.qsize()
            #     device_xml_node.set("count", str(all_count))
                status = queue.get(timeout=5)
                dev_status = v_data.DeviceStatusInfo()
                if status is not None and isinstance(status, str) and 0 < len(status):
                    status_node = ET.fromstring(status)
                    status_node.set("push_time", str(time.asctime(time.localtime(time.time()))))
                    device_xml_node.append(status_node)
                    #dev_status.DeviceID = "{0}:{1}".format(str(status_node.get('device_id')), str(status_node.get('ip')))
                    dev_status.DeviceID = (str(status_node.get('device_id')))
                    dev_status.DeviceIndex = 0
                    channel_node = status_node.get("channel")
                    if channel_node is not None:
                        dev_status.ChannelIndex = int(status_node.get("channel"))
                    else:
                        dev_status.ChannelIndex = 0
                    status_str = status_node.text
                    if str(status_str).lower() == "true":
                        dev_status.Status = 0
                    else:
                        dev_status.Status = 1
                    dev_status.ErrorCode = 0

                    def push_status_info_to_db(service_id, sqlhelper, dev_status):
                        if sqlhelper is not None and dev_status is not None:
                            try:
                                device_status_table = DBTypes.PushStatusInfoTable()
                                device_status_table.deviceID = dev_status.DeviceID
                                device_status_table.serviceID = service_id
                                device_status_table.channel = dev_status.ChannelIndex
                                device_status_table.pushTime = datetime.datetime.now()
                                device_status_table.errMsg = str(dev_status.ErrorCode)
                                if dev_status.Status == 0:
                                    device_status_table.status = True
                                else:
                                    device_status_table.status = False
                                sqlhelper.add(device_status_table)
                            except:
                                traceback.print_exc()

                    need_push_device_status_list.append(dev_status)
                    logger.info("client {0} Push status begin status:{1}".format(self._manuc, dev_status))
                    #推送失败，则持续推送
                    while self._center_session.PushDeviceStatus(dev_status):
                        time.sleep(2)
                    logger.info("client {0} Push status end".format(self._manuc))
                    #push_status_info_to_db(service_id, self._db_helper, dev_status)
                else:
                    logger.warn("client {0} status error status:{1}".format(self._manuc, status))
            #objgraph.show_growth()
            except Queue.Empty:
                if 0 < len(need_push_device_status_list):
                    # logger.info("client {0} Push status begin count:{1}".format(self._manuc, len(need_push_device_status_list)))
                    # self._center_session.PushDeviceStatusList(need_push_device_status_list)
                    # logger.info("client {0} Push status end".format(self._manuc))
                    all_count += len(need_push_device_status_list)
                    device_xml_node.set("count", str(all_count))
                    file=open(save_file,'a+')
                    etree.write(file, encoding="utf-8", method="xml")
                    file.close()
                    device_xml_node.clear()
                need_push_device_status_list[:] = []
                logger.info("time_out")
                continue
            except Exception,ex:
                logger.info("Exception:{0}".format(ex))
                traceback.print_exc()

    def _set_callback(self):
        self._callback_proxy = v_device.DeviceDispatchServiceV1Prx.checkedCast(self._conn)
        self._callback_client = DeviceCallbackI.deviceCallBackI()
        adapter = self._communicator.createObjectAdapter("")
        ident = Ice.Identity()
        ident.name = Ice.generateUUID()
        ident.category = ""
        self._call_back = DeviceCallbackI.deviceCallBackI()
        adapter.add(self._call_back, ident)
        adapter.activate()
        self._callback_proxy.ice_getConnection().setAdapter(adapter)
        self._callback_proxy.SubscribeCallback(ident)
        dest_func = getattr(self, "data_callback")
        if dest_func is not None:
            self._call_back.subscribe_ptz_cmd(dest_func)

    def _connect(self):
        while True:
            try:
                self._proxy = v_device.DeviceDispatchServiceV1Prx.checkedCast(self._conn)
                logger.info("{0} client start proxy:{1} conn_str:{2}".format(self._manuc, self._proxy, self._proxy_value))
                if self._proxy is not None:

                    self._center_session = self._proxy.register2(str(self._id), self._manuc)#session = DeviceDispatchSessionI.DeviceDispatchSessionI(unique_id)
                    logger.info("{0} client session create session:{1}.".format(self._id, self._center_session))
                keep_live_thrd = SessionKeepLiveThread(5, self._center_session, self)
                keep_live_thrd.start()
                return self._proxy
            except Ice.LocalException:
                traceback.print_exc()
                continue

    def _do_main_loop(self):
        try:
            new_device_lists = dict()
            device_lists = None
            while True:
                if (self._center_session):
                    device_lists = self.session.GetDeviceList()
                    logger.debug("length of device_list={0}".format(len(device_lists)))
                if device_lists is None or 1 > len(device_lists):
                    time.sleep(2)
                    continue
                new_device_lists.update(dict((device.DeviceID, device) for device in device_lists))
                same = False
                if len(new_device_lists) == len(self._whole_devices_list)\
                        and 0 == len(set(new_device_lists.keys()) - set(self._whole_devices_list.keys())):
                    same = True
                    time.sleep(2)
                    continue
                if not same:
                    #logger.debug("counts:{0}, lists:{1}".format(len(new_device_lists), new_device_lists))
                    id_str = ":".join(new_device_lists.keys())
                    logger.debug("counts:{0}, id_list:{1}".format(len(new_device_lists), id_str))
                    with self._dev_list_mutex:#mutex.acquire()
                        if len(self._whole_devices_list) != len(new_device_lists):
                            logger.warning("client {0} device changes".format(self._manuc))
                        add_list = self._add_devices(self._whole_devices_list, new_device_lists)
                        if 0 < len(add_list):
                            self._add_device_queue.put(add_list)
                        del_list = self._del_devices(self._whole_devices_list, new_device_lists)
                        if 0 < len(del_list):
                            self._del_device_queue.put(del_list)
                        alter_list = self._alter_devices(self._whole_devices_list, new_device_lists)
                        if 0 < len(alter_list):
                            self._alter_device_queue.put(alter_list)
                        if 0 < len(new_device_lists):
                            self._whole_devices_list.update(new_device_lists)
                        map(lambda dev_id: self._whole_devices_list.pop(dev_id), del_list.keys())
                    #结束后mutex.realise()
                    #logger.debug("{0} client: all-device counts:{1} get device counts:{2}".format(self._manuc, len(self._whole_devices_list), len(new_device_lists)))
                    logger.debug("whole list count:{0} id:{1}".format(len(self._whole_devices_list), id(self._whole_devices_list)))
                    #？？？？？？？？？？？这样队列中是否有许多相同的设备呢
                    if 0 < len(new_device_lists):
                        self.WholeDevListQueue.put(add_list.values())
                    #device_change_str = "{0} clients: adddevicescounts:{1} delcounts:{2} altercounts:{3}.".format(self._manuc, len(add_list), len(del_list), len(alter_list))
                    #logger.debug(device_change_str)
                    #out_str = "{0} clients: addqueuecounts:{1} delqueue:{2} alterqueue:{3}.".format(self._manuc, self._add_device_queue.qsize(), self._del_device_queue.qsize(), self._alter_device_queue.qsize())
                    #logger.debug(out_str)
                new_device_lists.clear()
                #???????????????????
                objgraph.show_growth()
        except Exception as e:
            traceback.print_exc()
            # print("exception:", e)
            # keep_live_thrd.terminate()
            # keep_live_thrd.join()
        # finally:
        #     keep_live_thrd.terminate()
        #     keep_live_thrd.join()
        logger.debug("client {0} center session:{1}".format(self._manuc, self._center_session))
        self._communicator.waitForShutdown()

    def _do_register_device(self):
        while 1:
            while not self._add_device_queue.empty():
                device_lists = self._add_device_queue.get()
                logger.info("{0} client Begin Register Device, counts:{1}".format(self._manuc, len(device_lists)))
                register_result = map(self._register_device, device_lists.values())
                logger.info("{0} client End Register Device, counts:{1}".format(self._manuc, len(device_lists)))
            while not self._del_device_queue.empty():
                del_dev_lists = self._del_device_queue.get()
                logger.info("{0} client Begin unRegister Device, counts:{1}".format(self._manuc, len(del_dev_lists)))
                unregister_result = map(self._unregister_device, del_dev_lists.values())
                logger.info("{0} client End unRegister Device, counts:{1}".format(self._manuc, len(del_dev_lists)))
            #objgraph.show_growth()
            time.sleep(0.01)

    def _do_register_device_bythreadpool(self):
        """
        从队列中获取新增和删除的设备，开启线程对其进行处理
        :return:
        """
        task_pool = threadpool.ThreadPool(32)
        request_list = []
        func = getattr(self, "_register_device")
        unfunc = getattr(self, "_unregister_device")
        while 1:
            all_add_device_lists = {}
            while not self._add_device_queue.empty():
                add_device_lists = self._add_device_queue.get()
                all_add_device_lists.update(add_device_lists)
            add_request_tasks = []
            for device in all_add_device_lists.values():
                add_request_tasks.extend(threadpool.makeRequests(func, [((device, ), {})]))
            if 0 < len(add_request_tasks):
                logger.info("{0} client Begin Register Device, counts:{1}".format(self._manuc, len(all_add_device_lists)))
                map(task_pool.putRequest, add_request_tasks)
                logger.info("{0} client End Register Device, counts:{1}".format(self._manuc, len(all_add_device_lists)))

            all_del_device_lists = {}
            while not self._del_device_queue.empty():
                del_dev_lists = self._del_device_queue.get()
                all_del_device_lists.update(del_dev_lists)
            del_request_tasks = []
            for device in all_del_device_lists.values():
                del_request_tasks.extend(threadpool.makeRequests(unfunc, [((device,), {})]))
            if 0 < len(del_request_tasks):
                logger.info("{0} client Begin unRegister Device, counts:{1}".format(self._manuc, len(all_del_device_lists)))
                unregister_result = map(task_pool.putRequest, del_request_tasks)
                logger.info("{0} client End unRegister Device, counts:{1}".format(self._manuc, len(all_del_device_lists)))
            task_pool.wait()  # 等待所有处理完成后返回
            time.sleep(0.1)

    def _do_register_device_byeventlet(self):
        task_pool = eventlet.GreenPool(500)
        while 1:
            while not self._add_device_queue.empty():
                device_lists = self._add_device_queue.get()
                dev_id_str = ":".join(device_lists.keys())
                logger.info("{0} client Begin Register Device, counts:{1} id:{2}.".format(self._manuc, len(device_lists), dev_id_str))
                register_func = getattr(self, "_register_device")
                for item in device_lists.values():
                    task_pool.spawn_n(register_func, item)
                #task_pool.waitall()
                # if register_func is not None:
                #     result = task_pool.imap(register_func, device_lists.values())
                #     for item in result:
                #         pass
                    # task_pool.waitall()
                logger.info("{0} client End Register Device, counts:{1}".format(self._manuc, len(device_lists)))
            while not self._del_device_queue.empty():
                del_dev_lists = self._del_device_queue.get()
                logger.info("{0} client Begin unRegister Device, counts:{1}".format(self._manuc, len(del_dev_lists)))
                unregister_func = getattr(self, "_unregister_device")
                for item in device_lists.values():
                    task_pool.spawn_n(unregister_func,item)
                # if unregister_func is not None:
                #     task_pool.imap(unregister_func, del_dev_lists.values())
                #     task_pool.waitall()
                logger.info("{0} client End unRegister Device, counts:{1}".format(self._manuc, len(del_dev_lists)))
            time.sleep(0.01)

    def stream_uri_callback(self,request, result):
        logger.info("stream call back request:{0} result:{1}".format(request, result))
        if result is not None and isinstance(result, tuple) and 0 < len(result[0]):
            try:
                urls_node = ET.fromstring(result[0])
                channel_urls_dict={}
                for stream_url_node in urls_node.iter("stream_url"):
                    stream=v_device.DeviceStreamInfo
                    stream_url_id = stream_url_node.get("id")
                    id_list = str(stream_url_id).split(":")
                    deviceID = str(id_list[0])
                    channel = str(id_list[1])
                    channel_key=deviceID+"_"+channel
                    if not channel_urls_dict.has_key(channel_key):
                        channel_urls=v_device.DeviceChannelInfo()
                        channel_urls_dict.setdefault(channel_key,channel_urls)
                        channel_urls.deviceID=cstr(id_list[0])
                        channel_url_user = stream_url_node.get("user_name")
                        channel_url_pwd = stream_url_node.get("password")
                        channel_urs.username = str(channel_url_user)
                        channel_urls.password = str(channel_url_pwd)
                        channel_url_third = stream_url_node.get("third_party")
                        if str(channel_url_third) == str(True):
                            channel_urls.thirdparty = True
                        else:
                            channel_uris.thirdparty = False
                    stream.channel=int(id_list[1])
                    stream.stream = int(id_list[2])
                    stream.deviceID=str(id_list[0])
                    stream.uri = str(stream_url_node.text)
                    if not channel_urls_dict[channel_key].streamList:
                        channel_urls_dict[channel_key].streamList=[]
                    channel_urls_dict[channel_key].streamList.append(stream)
                    if(deviceID in self._failed_url_device):
                        self._failed_url_device.remove(stream_uri.deviceID)
                self.StreamUriQueue.put(channel_urls_dict)
                #self.StreamUriQueueDB.put(stream_urls)
            except:
                traceback.print_exc()
        else:
            if request.args[0] in self._whole_devices_list.keys():
                self._failed_url_device.update([request.args[0]])
            else:
                if request.args[0] in self._failed_url_device:
                    self._failed_url_device.remove(request.args)
            logger.error("request:{0} failed, result:{1}".format(request, result))

    def stream_uri_exception_callback(self,request, exc_info):
        logger.error("Get Stream uri fail request:{0} err_info:{1}".format(request, exc_info))

    def _doGetStreamUriByEventlet(self):
        get_stream_uri_pool = eventlet.greenpool.GreenPool()
        cur_dev_lists = []
        while True:
            while not self.WholeDevListQueue.empty():
                tmp_list = self.WholeDevListQueue.get()
                cur_dev_lists = list(set(cur_dev_lists)|set(tmp_list))
            if 0 < len(cur_dev_lists):
                begin_time = time.time()
                # logger.info("{0} client Get Stream URI dev_list count:{1}.".format(self._manuc, len(cur_dev_lists)))
                result_list = []
                for device in cur_dev_lists:
                    get_stream_uri = "http://{0}:{1}/device?func=get_stream_url".format(str(device.IP), str(device.Port))
                    func = None
                    if device.ProtocolFlag == VProtocolType.SDK:
                        if device.Manufacture == VManufacture.HIKVISION:
                            func = getattr(vistek_hikvision, "request_cmd")
                    elif device.ProtocolFlag == VProtocolType.ONVIF:
                        func = getattr(vistek_onvif, "request_cmd")
                    elif device.ProtocolFlag == VProtocolType.PSIA:
                        func = getattr(vistek_psia, "request_cmd")
                    else:
                        logger.error("client {0} NO Protocol finded, device_id:{1} protocol_flag:{2}" \
                                     .format(self._manuc, device.DeviceID, device.ProtocolFlag))
                    if func is not None:
                        result_list.append(get_stream_uri_pool.spawn(func, device.DeviceID, self.get_stream_uri, ''))
                result = [waiter.wait() for waiter in result_list]
                # logger.info("{0} client Get Stream URI End dev_list count:{1}.".format(self._manuc, len(cur_dev_lists)))
                end_time = time.time()
                if 0 < len(cur_dev_lists):
                    logger.info("{0} client total get {1} device stream_uri time:{2}".format(self._manuc\
                                                                                         , len(cur_dev_lists)\
                                                                                         , (end_time-begin_time)))
                map(lambda item:self.StreamUriQueue.put(item), result)
            #objgraph.show_growth()
            time.sleep(60)

    def _doGetStreamUri(self):
        """
        每隔60s获取新设备的流地址
        :return:
        """
        get_stream_uri_pool = threadpool.ThreadPool(num_workers=8)
        cur_dev_lists = []
        tmp_list_dict = {}
        # count = 0
        while True:
            work_req_list = []
            cur_dev_lists = []
            map(lambda deviceID: tmp_list_dict.pop(deviceID),
                list(set(tmp_list_dict.keys()) - set(self._failed_url_device)))
            while not self.WholeDevListQueue.empty():
                tmp_list = self.WholeDevListQueue.get()
                tmp_list_dict.update(dict((device.DeviceID, device) for device in tmp_list))
            cur_dev_lists = tmp_list_dict.values()  # 内容比较有问题造成内存增
            if 0 < len(cur_dev_lists):
                begin_time = time.time()
                for device in cur_dev_lists:
                    get_stream_uri = "http://{0}:{1}/device?func=get_stream_url".format(str(device.IP), str(device.Port))
                    func = None
                    if device.ProtocolFlag == VProtocolType.SDK:
                        if device.Manufacture == VManufacture.HIKVISION:
                            func = getattr(vistek_hikvision, "request_cmd")
                    elif device.ProtocolFlag == VProtocolType.ONVIF:
                        func = getattr(vistek_onvif, "request_cmd")
                    elif device.ProtocolFlag == VProtocolType.PSIA:
                        func = getattr(vistek_psia, "request_cmd")
                    else:
                        logger.error("client {0} NO Protocol finded, device_id:{1} protocol_flag:{2}"\
                                     .format(self._manuc, device.DeviceID, device.ProtocolFlag))
                    if func is not None:
                        work_req_list.extend(threadpool.makeRequests(func, [((device.DeviceID,get_stream_uri, []), {})]\
                                                                           , self.stream_uri_callback, self.stream_uri_exception_callback))
            if 0 < len(work_req_list):
                map(get_stream_uri_pool.putRequest, work_req_list)
                get_stream_uri_pool.wait()
                end_time = time.time()
                logger.info("{0} client total get {1} device stream_uri time:{2}".format(self._manuc \
                                                                                         , len(cur_dev_lists) \
                                                                                         , (end_time-begin_time)))
            # refer_count = sys.getrefcount(work_req_list)
            del work_req_list[:]
            time.sleep(2)

    def _doPushStreamUriByEventlet(self):
        global report_data_path
        stream_urls = {}
        need_to_push_stream_url = {}
        stream_url_nodes = ET.Element("devicestreamuri")
        etree = ET.ElementTree(stream_url_nodes)
        while 1:
            all_device_counts = 0
            if 0 < self.StreamUriQueue.qsize():
                all_device_counts += self.StreamUriQueue.qsize()
                stream_url_nodes.set("device_count", str(all_device_counts))

            while not self.StreamUriQueue.empty():
                result = self.StreamUriQueue.get()
                if result is not None and isinstance(result, tuple) and 0 < len(result[0]):
                    try:
                        urls_node = ET.fromstring(result[0])
                        stream_url_nodes.extend([urls_node])
                        for stream_url_node in urls_node.iter("stream_url"):
                            stream_uri = v_device.DeviceStreamInfo()
                            stream_url_id = stream_url_node.get("id")
                            id_list = str(stream_url_id).split(":")
                            stream_uri.deviceID = str(id_list[0])
                            stream_uri.uri = str(stream_url_node.text)
                            stream_url_user = stream_url_node.get("user_name")
                            stream_url_pwd = stream_url_node.get("password")
                            stream_url_third = stream_url_node.get("third_party")
                            stream_uri.username = str(stream_url_user)
                            stream_uri.password = str(stream_url_pwd)
                            stream_uri.channel = int(id_list[1])
                            stream_uri.stream = int(id_list[2])
                            if str(stream_url_third) == str(True):
                                stream_uri.thirdparty = True
                            else:
                                stream_uri.thirdparty = False
                            if stream_url_id not in stream_urls:
                                need_to_push_stream_url.update({stream_url_id:stream_uri})
                            stream_urls.update({stream_url_id:stream_uri})
                    except:
                        traceback.print_exc()
            if 0 < len(need_to_push_stream_url):
                cur_time = str(time.strftime("%Y-%m-%d-%H-%M-%S", time.localtime(time.time())))
                file_name = "streamurl-{0}-{1}-{2}.xml".format(self._manuc, os.getpid(), cur_time)
                save_file = os.path.join(report_data_path, file_name)
                etree.write(save_file, encoding='utf-8', method="xml")
                logger.info("client {0} Push Stream URI Begin".format(self._manuc))
                #self._proxy.PushDeviceStreamInfos(stream_uri_map)
                self._proxy.PushDeviceStreamInfos(need_to_push_stream_url)
                logger.info("client {0} Push Stream URI success len:{1} url:{2}".format(self._manuc \
                                                                                     , len(need_to_push_stream_url)\
                                                                                     , need_to_push_stream_url))
            #objgraph.show_growth()
            need_to_push_stream_url.clear()
            time.sleep(0.01)

    def _doPushStreamUri(self):
        stream_urls = {}
        stream_urls_node = ET.Element("streamurls")
        etree = ET.ElementTree()
        while self._push_stream_uri_thrd and self._push_stream_uri_thrd.is_alive():
            stream_uri_map = self.StreamUriQueue.get()
            need_to_push_stream_url = [item for key, item in stream_uri_map.items() if key not in stream_urls]
            stream_urls_node.set("count", str(len(need_to_push_stream_url)))
            if 0 < len(need_to_push_stream_url):
                # for key, value in need_to_push_stream_url.items():
                #     channel_node = ET.SubElement(stream_urls_node, "url")
                #     channel_node.text =  str(value.uri)
                #     channel_node.set("channel", str(value.channel))
                #     channel_node.set("stream", str(value.stream))
                # ET.SubElement(stream_urls_node, "url").text = value for key, value in need_to_push_stream_url.items()
                # cur_time = str(time.strftime("%Y-%m-%d-%H-%M-%S", time.localtime(time.time())))
                # file_name = "streamurl-{0}-{1}-{2}.xml".format(self._manuc, os.getpid(), cur_time)
                # save_file = os.path.join(report_data_path, file_name)
                # etree.write(save_file, encoding='utf-8', method="xml")
                service_id = "{0}:{1}".format(self._manuc, os.getpid())
                def insert_push_info(service_id, sqlhelper, push_stream_url):
                    try:
                        if sqlhelper is not None and push_stream_url is not None:
                            stream_info = DBTypes.PushStreamUrlInfoTable()
                            stream_info.serviceID = service_id
                            stream_info.urlCount = len(need_to_push_stream_url)
                            url_content_list = [item.uri for item in need_to_push_stream_url.values()]
                            urls_content = ",".join(url_content_list)
                            stream_info.urlContent = urls_content
                            stream_info.pushTime = datetime.datetime.now()
                            sqlhelper.add(stream_info)
                    except:
                        traceback.print_exc()
                #insert_push_info(service_id, self._db_helper, need_to_push_stream_url)
                stream_urls.update({item for key, item in stream_uri_map.items() if key not in stream_urls})
                logger.info("client {0} Push Stream URI Begin".format(self._manuc))
                #self._proxy.PushDeviceStreamInfos(stream_uri_map)
                # self._proxy.PushDeviceStreamInfos(stream_uri_map)
                while self._proxy.PushDeviceStreamInfos(need_to_push_stream_url):
                    time.sleep(2)
                logger.info("client {0} Push Stream URI success len:{1} ".format(self._manuc\
                                                                                 , len(need_to_push_stream_url)))
                # logger.info("client {0} Push Stream URI success len:{1} map:{2}".format(self._manuc\
                #                                                             , len(stream_uri_map)\
                #                                                             , stream_uri_map))

    def _doGetServiceStatusInfoIntoSqlite(self):
        logger.info("push status into sqlite")
        while 1:
            service_id = "{0}:{1}".format(self._manuc, os.getpid())
            phy_info = watch_types.PhysicsDeviceInfo()
            service_load_info = watch_types.ServiceLoadInfo(service_id=service_id)
            register_info = None
            if self._manuc == "hikvision":
                register_info = vistek_hikvision.getCurrentDeviceInfo()
            if self._manuc == "psia":
                register_info = vistek_psia.getCurrentDeviceInfo()
            # if self._manuc == "onvif":
            #     register_info = vistek_onvif.getCurrentDeviceInfo()
                # time.sleep(60)
                # continue
            if register_info is not None:
                register_info_table = DBTypes.DeviceRegisterInfoTable()
                register_info_table.serviceID= service_id
                register_info_table.allDeviceCount = register_info[0]
                register_info_table.registerSuccesscount = register_info[1]
                register_info_table.registerFailcount = register_info[2]
                register_info_table.registerSuccessDeviceList = register_info[3]
                register_info_table.registerFailDeviceList = register_info[4]
                if self._db_helper is not None:
                    self._db_helper.add(register_info_table)
                service_loading_info_table = DBTypes.ServiceLoadingInfoTable()
                service_loading_info_table.serviceID = service_id
                service_loading_info_table.cpuUseRate = service_load_info._cpu_info.get(watch_types.CPU_PERCENT_LABEL, None)
                service_loading_info_table.memUseRate = service_load_info._mem_info.get(watch_types.MEM_RATE_LABEL)
                service_loading_info_table.availableMemory = float(phy_info._free_mem)
                service_loading_info_table.useMemory = service_load_info._mem_info.get(watch_types.MEM_USAGE_LABEL)
                service_loading_info_table.threadCount = service_load_info._run_info.get(watch_types.RUN_NUM_THREAD)
                if self._db_helper is not None:
                    self._db_helper.add(service_loading_info_table)
                server_device_info_table = DBTypes.DeviceServerInfoTable()
                server_device_info_table.serverIP = str(socket.gethostbyname(socket.gethostname()))
                server_device_info_table.cpuCount = phy_info._cpu_count
                server_device_info_table.physicalCpuCount = phy_info._phy_cpu_count
                server_device_info_table.memSize = phy_info._total_mem
                server_device_info_table.availableMemory = phy_info._free_mem
                server_device_info_table.memUseRate = phy_info._mem_rate
                server_device_info_table.netReceiving = float(0.0)
                server_device_info_table.netSending = float(0.0)

                def update_server_info(session, table_info):
                    if session is not None:
                        session.query(DBTypes.DeviceServerInfoTable)\
                                        .filter(table_info.serverIP == DBTypes.DeviceServerInfoTable.serverIP)\
                                        .update({DBTypes.DeviceServerInfoTable.availableMemory:table_info.availableMemory\
                                        , DBTypes.DeviceServerInfoTable.memUseRate:table_info.memUseRate})
                def select_server_info(session, table_info):
                    if session is not None:
                        result = session.query(DBTypes.DeviceServerInfoTable)\
                                        .filter(table_info.serverIP == DBTypes.DeviceServerInfoTable.serverIP).all()
                        return  result
                if self._db_helper is not None:
                    result = self._db_helper.exe_cmd(select_server_info, server_device_info_table)
                    if result is not None and 0 < len(result):
                        self._db_helper.update(update_server_info, server_device_info_table)
                    else:
                        self._db_helper.add(server_device_info_table)
            time.sleep(60)

    def _doPushStreamUriToMysql(self):
        while True:
            urlStructList=[]
            try:
                urls=self.StreamUriQueueDB.get(timeout=1)
                for url_id,url in urls.items():
                    urlStruct=DBTypes.UrlTableData()
                    url_id_list=url_id.split(":")
                    urlStruct.deviceID=url_id_list[0]
                    urlStruct.channel=url_id_list[1]
                    urlStruct.streamType=url_id_list[2]
                    urlStruct.url=url.uri
                    urlStruct.protocol=self._manuc
                    urlStructList.append(urlStruct)
                logger.info("length of urlStructList:{0}".format(len(urlStructList)))
                self._db_storage.putStreamUriToTable(urlStructList)
            except Queue.Empty:
                continue





    def _doGetServiceStatusInfo(self):
        while 1:
            service_id = "{0}:{1}".format(self._manuc, os.getpid())
            phy_info = watch_types.PhysicsDeviceInfo()
            service_load_info = watch_types.ServiceLoadInfo(service_id=service_id)
            if self._manuc == "hikvision":
                register_info = vistek_hikvision.getCurrentDeviceInfo()
            register_info_tuple = (service_id,) + register_info
            register_info_name_tuple = ("serviceID", "allDeviceCount", "registerSuccessCount"\
                                        , "registerFailCount", "registerSuccessDeviceList"\
                                        , "registerFailDeviceList")
            register_info_name_str = "".join(",").join(register_info_name_tuple)
            insert_cmd = "insert into {0}.{1} ({2}) values{3}".format(self._db_helper._dbname\
                                                                        , REGISTER_TABLE_NAME
                                                                        , register_info_name_str
                                                                        , register_info_tuple)
            self._db_helper.exe_cmd(insert_cmd)
            # waiter = eventlet.spawn(self._db_helper.exe_cmd, insert_cmd)
            # waiter.wait()
            #service loading info
            cpu_rate = service_load_info._cpu_info.get(watch_types.CPU_PERCENT_LABEL, None)

            loading_info_value_tuple = (service_id,) + (cpu_rate,)\
                                 + (service_load_info._mem_info.get(watch_types.MEM_RATE_LABEL)\
                                         , service_load_info._mem_info.get(watch_types.MEM_USAGE_LABEL))\
                                 + (float(phy_info._free_mem),)\
                                 + (service_load_info._run_info.get(watch_types.RUN_NUM_THREAD),)
            loading_info_name_tuple = ("serviceID", "cpuUseRate", "memUseRate", "useMemory"\
                                       , "availableMemory", "threadCount")
            loading_info_str = "".join(",").join(loading_info_name_tuple)
            insert_loading_cmd = "insert into {0}.{1} ({2}) values{3}".format(self._db_helper._dbname \
                                                                      , LOADINFO_TABLE_NAME
                                                                      , loading_info_str
                                                                      , loading_info_value_tuple)
            self._db_helper.exe_cmd(insert_loading_cmd)

            #device Server info.
            ip = socket.gethostbyname(socket.gethostname())
            server_value_tuple = (ip, phy_info._cpu_count, phy_info._phy_cpu_count\
                , float(phy_info._total_mem), float(phy_info._free_mem), float(phy_info._mem_rate)\
                , float(0.0), float(0.0))
            server_info_name_tuple = ("serverIP", "cpuCount", "physicalCpuCount", "memSize", "availableMemory"\
                                      , "memUseRate", "netReceiving", "netSending")
            server_info_name_str = "".join(",").join(server_info_name_tuple)
            insert_server_info_cmd = "insert ignore into {0}.{1} ({2}) values{3}".format(self._db_helper._dbname \
                                                                      , SERVER_INFO_TABLE_NAME
                                                                      , server_info_name_str
                                                                      , server_value_tuple)
#             insert_server_info_cmd = "insert into {0}.{1} ({2}) select {3} from dual where not exists \
# (select {4} from {5} where {6}.{7}={8})".format(self._db_helper._dbname\
#                                                   , SERVER_INFO_TABLE_NAME\
#                                                   , server_info_name_str\
#                                                   , server_value_tuple\
#                                                   , "serverIP"\
#                                                   , SERVER_INFO_TABLE_NAME\
#                                                   , SERVER_INFO_TABLE_NAME\
#                                                   , "serverIP"\
#                                                   , str(ip))
            self._db_helper.exe_cmd(insert_server_info_cmd)
            time.sleep(60)
        #watch_types.CurSeviceStatusInfo(service_id=service_id, status=)

class MySqlDbHelper():
    def __init__(self, host, user, pwd, dbname):
        self._host, self._user, self._pwd, self._dbname  = host, user, pwd, dbname
        self._connecter = MySQLdb.connect(host=self._host\
                                          , user=self._user\
                                          , passwd=self._pwd\
                                          , db=self._dbname\
                                          , charset='utf8')
        self._connecter.autocommit(on=True)
        self._exe_handle = self._connecter.cursor()
    def is_cmd_available(self, cmd):
        return True
    def exe_cmd(self, cmd):
        if self._exe_handle is not None and self.is_cmd_available(cmd):
            count = self._exe_handle.execute(cmd)
            result_rows = []
            if 0 < count:
                result_rows.extend([row for row in self._exe_handle.fetchall()])
            return (count, result_rows)
        return None


# def parse_db_config_file(db_file):
#     if db_file is None or not os.path.exists(db_file):
#         raise Exception, "Error:mysql config file is None or db_file is not exists"
#     db = collections.namedtuple("parameters", "type host port user passwd db table")
#     db_tree = ET.ElementTree()
#     db_tree.parse(db_file)
#     root_node = db_tree.getroot()
#     if root_node is not None:
#         db_type = root_node.get("dbtype")
#         if db_type == "mysql":
#             db_para = db(type=root_node.get("type")
#                          , host=root_node.get("host")
#                          , port=root_node.get("port")
#                          , passwd=root_node.get("passwd")
#                          , user=root_node.get("user")
#                          , db=root_node.get("db")
#                          , table=root_node.get("table"))
#             return db_para
#     else:
#         raise Exception, "db config file error"
class MysqlDB_init():
    def __init__(self,db_para):
        self.db_para=db_para
        self.conn=None
        self.cursor=None
        self.DB_init()
    def DB_init(self):
        self.conn = self.Newconnection()
        if self.conn is None:
            raise Exception,"ERROR:connecting mysql occur error"
        self.setAutocommit(True)
        self.cursor=self.getCursor()
        self.createDatabase()
        self.setDatabase()
        self.createTable()
        self.createStorageProcess()

    def Newconnection(self):
        try:
            conn=MySQLdb.connect(host=self.db_para.host,port=int(self.db_para.port),user=self.db_para.user,passwd=self.db_para.passwd)
            return conn
        except Exception,ex:
            traceback.print_exc()
            return None
    def closeConnection(self):
        if self.conn is not None:
            self.conn.close()
    def getCursor(self):
        return self.conn.cursor()
    def setAutocommit(self,type):
        if self.conn is not None:
            self.conn.autocommit(on=type)
    def setDatabase(self):
        sqltext="use %s" % self.db_para.db
        self.execute(sqltext)
    def createDatabase(self):
        seltext="create database if not exists %s" % self.db_para.db
        self.execute(seltext)
    def createTable(self):
        sqltext="create table if not exists %s(protocol VARCHAR(10)" \
                ",deviceID VARCHAR(100)" \
                ",channel VARCHAR(10)" \
                ",streamType VARCHAR(10)" \
                ",url VARCHAR(200)" \
                ",createDate datetime" \
                ",updateDate datetime)" % self.db_para.table
        self.execute(sqltext)
    def execute(self,sqltext):
        if self.cursor is not None:
            try:
                line=self.cursor.execute(sqltext)
            except Exception,ex:
                traceback.print_exc()
                return None
        return line
    def createStorageProcess(self):
        sql="show procedure status like 'demo_proc'"
        rows=self.execute(sql)
        if rows ==0:
            sql = "CREATE PROCEDURE demo_proc(IN protocol_in VARCHAR(10) ,IN deviceID_in VARCHAR(100) ,IN channel_in VARCHAR(10) ,IN streamType_in VARCHAR(10) ,IN url_in VARCHAR(200)) \
            BEGIN \
                DECLARE n int DEFAULT 0;\
                SELECT COUNT(*) INTO n FROM streamurl WHERE protocol=protocol_in AND deviceID=deviceID_in AND channel=channel_in AND streamType=streamType_in;\
                if n=0 THEN \
                INSERT INTO streamurl(protocol,deviceID,channel,streamType,url,createDate,updateDate) VALUES (protocol_in,deviceID_in,channel_in,streamType_in,url_in,now(),now());\
                ELSE \
                UPDATE streamurl set url=url_in,updateDate=now() WHERE protocol=protocol_in AND deviceID=deviceID_in AND channel=channel_in AND streamType=streamType_in; \
                END if; \
                END;"
            self.execute(sql)

    def putStreamUriToTable(self,urlStructList):
        for uri in urlStructList:
            logger.info("uri:{0}".format(uri.streamType))
            result=self.cursor.callproc("demo_proc",(uri.protocol,uri.deviceID,uri.channel,uri.streamType,uri.url))
            self.conn.commit()
            logger.info("resutl:({0}".format(result))




if __name__ == "__main__":
    # center_client = DispatchClient("device_dispatch_service:tcp -h localhost -p 54321")
    # center_client.start()
    db_para=parse_db_config_file("C:\\Users\\Administrator\\Desktop\\start_script\\db_config.xml")
    instance=MysqlDB_init(db_para)
    raw_input()

