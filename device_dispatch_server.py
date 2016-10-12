#!/usr/bin/env python
# -*- coding=utf-8 -*-

import Ice, logging, logging.handlers, sys, threading, time, os
from vistek_util.threadTemplate import ReapThread
import device_center_client

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
formatter = logging.Formatter(
    "[%(asctime)s] [%(levelname)s] [%(name)s] [%(filename)s:%(funcName)s:%(lineno)s]  [%(message)s]")

handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(log_level)
import vistek_util.DeviceDispatchServiceI as DeviceDispatchServiceI

def enum(**enums):
    return type('Enum', (), enums)
VProtocolType = enum(EMPTY=0, SDK=1, ONVIF=2, PSIA=4,  GB28181=8, HDCCTV=16)

def reduce_min_device_session(session, session_next):
    if session.DeviceListsCount() <= session_next.DeviceListsCount():
        return session
    else:
        return session_next


class DispatchServer():
    def __init__(self, name, center_config, server_config_file):
        """

        :param name:
        :param center_config:
        :param server_config_file:
        """
        self._name = name#dispatch
        self._data_sender = None
        self._server_config = server_config_file#dispatch.Endpoints=default -h localhost -p 0
        self._adapter = None
        init_data = Ice.InitializationData()
        init_data.properties = Ice.createProperties()
        init_data.properties.load(server_config_file)
        self._communicator = Ice.initialize(sys.argv, init_data)
        self._dispatch_service = None#self._dispatch_service = DeviceDispatchServiceI.DeviceDispatchServiceI(self._reapter, self._name)
        self._center_config = center_config#device_dispatch_service:tcp -h 172.16.0.80 -p 54321
        self._center_client = device_center_client.DeviceCenterClient(self._center_config)
        self._connect_str = None
        self._do_seesion_map = dict()#self._do_seesion_map[device_id] = do_session 记录哪个设备被哪个session处理
        self._reapter = None#线程检测_sessions中超时的对话，并关闭
        self._dispatch_device_to_session_thrd = threading.Thread(target=DispatchServer._dispatchDevicesToSession,
                                                                 name="dispatch_device_to_session", args={self, })
        #生成adapter,以及proxy object，同时单独起线程，检测session的超时情况，超时则移除
        self._do_start_server_thrd = threading.Thread(target=DispatchServer._do_start, name="do_start", args={self, })
        self._push_session = None#self._center_client._center_session
        self._set_push_session_thrd = None
        self._is_start = False
        self._undispatch_devices = dict()#未被session处理的device

    @property
    def device_count(self):
        """
        :return dict
        """
        if self._center_client and 0 < len(self._center_client.device_counts):
            return self._center_client.device_counts()

    @property
    def   is_start(self):
        return self._is_start

    def _dispatchDevicesToSession(self):
        """
        每隔0.01秒从center_client中获取增加和删除的设备，将增加的设备push到设备最少的session中，从session中删除删除的设备
        :rtype: None
        :distpatch all device from the center to incoming the session.
        """
        while self._is_start:
            if self._center_client and 0 < len(self._dispatch_service.sessionMap()):
                while not self._center_client.addDeviceQueue.empty():
                    device_lists = self._center_client.addDeviceQueue.get()
                    for device_id, device in device_lists.items():
                        session_map = list()
                        for id, session in self._dispatch_service.sessionMap().items():
                            if device.ProtocolFlag == VProtocolType.PSIA and "psia" in id:
                            #if device.ProtocolFlag == VProtocolType.PSIA:
                                session_map.append(session)
                            if device.ProtocolFlag == VProtocolType.ONVIF and "onvif" in id:
                            #if device.ProtocolFlag == VProtocolType.ONVIF:
                                session_map.append(session)
                            if device.ProtocolFlag == VProtocolType.SDK and device.Manufacture in id:
                                session_map.append(session)
                        do_session = None
                        if 0 < len(session_map):
                            #找出session中设备数量最少的session
                            do_session = reduce(reduce_min_device_session, session_map)
                            do_session.PushDevice(device_id, device)
                            self._do_seesion_map[device_id] = do_session
                            logger.debug("dosessionName:{0} device:{1} count:{2}".format(session.getName() \
                                                                                    , device \
                                                                                    , len(self._do_seesion_map)))
                        else:
                            self._undispatch_devices[device_id] = device
                            logger.warn("undo dispatch deviceid:{0} ip:{1} count:{2}".format(device.DeviceID\
                                                                                             , device.IP\
                                                                                             , len(self._undispatch_devices)))
                for device_id, device in self._undispatch_devices.items():
                    session_map = list()
                    for id, session in self._dispatch_service.sessionMap().items():
                        #if device.Manufacture in id:
                        #if device.ProtocolFlag == VProtocolType.PSIA and "psia" in id:
                        if device.ProtocolFlag == VProtocolType.PSIA and "psia" in id:
                            # if device.ProtocolFlag == VProtocolType.PSIA:
                            session_map.append(session)
                        if device.ProtocolFlag == VProtocolType.ONVIF and "onvif" in id:
                            # if device.ProtocolFlag == VProtocolType.ONVIF:
                            session_map.append(session)
                        if device.ProtocolFlag == VProtocolType.SDK and device.Manufacture in id:
                            session_map.append(session)
                    do_session =None
                    if 0 < len(session_map):
                        do_session = reduce(reduce_min_device_session, session_map)
                        do_session.PushDevice(device_id, device)
                        self._do_seesion_map[device_id] = do_session
                        logger.debug("dosessionName:{0} device:{1} count:{2}".format(session.getName() \
                                                                                , device \
                                                                                , len(self._do_seesion_map)))
                        self._undispatch_devices.pop(device_id)

                while not self._center_client.delDeviceQueue.empty():
                    device_lists = self._center_client.delDeviceQueue.get()
                    for device_id, device in device_lists.items():
                        if device_id in self._do_seesion_map:
                            logger.info("dispatch pop device dev_id:{0} ip:{1}".format(device_id, device.IP))
                            self._do_seesion_map[device_id].PopDevice(device_id)
                time.sleep(0.01)

    def _setPushSession(self,session):
        while self._is_start:
            session=self._center_client.session
            if self._center_client and 0 < len(self._dispatch_service.sessionMap()):
                for id, session_item in self._dispatch_service.sessionMap().items():
                    session_item.SetPushSession(session)
            time.sleep(1)

    def dispatch_svc(self):
        return self._dispatch_service

    def msg_callback(self, *args, **kwargs):
        pass

    def conn_str(self):
        endporints_lists = self._adapter.getEndpoints()
        if 0 < len(endporints_lists):
            self._connect_str = "{0}:{1}".format(self._name, endporints_lists[0].toString())
        return self._connect_str

    def subscribe_data_callback(self, func):
        self._data_sender = func

    def data_callback(self, *args, **kwargs):
        if self._data_sender is not None:
            print("server ---send")
            self._data_sender(*args, **kwargs)

    def start(self):
        self._center_client.start()  # boot get all devices client.
        #func = getattr(self, "data_callback")
        #self._center_client.subscribe_data_callback(func)
        time.sleep(5)
        self._do_start_server_thrd.start()  # boot the main server.
        time.sleep(5)
        self._push_session = self._center_client.session  # set the pushsession of all incoming seesions.
        push_session = self._center_client.session
        if self._push_session:
            #向sdk onvif psia session中设置_push_session
            self._set_push_session_thrd = threading.Thread(target=DispatchServer._setPushSession,
                                                           name="set_push_session", args=(self,self._push_session))
            self._set_push_session_thrd.start()
        self._dispatch_device_to_session_thrd.start()  # dispatch all device into all incoming sessions.

    def turn_on_subscribe(self):
        func = getattr(self._dispatch_service, "data_callback")
        #self.subscribe_data_callback(func)
        if self._center_client is not None:
            self._center_client.subscribe_data_callback(func)

    def _do_start(self):
        self._adapter = self._communicator.createObjectAdapter(self._name)
        self._reapter = ReapThread(time_out=30)#检测_sessions中超时的对话，并关闭
        self._reapter.start()
        try:
            self._is_start = True
            self._dispatch_service = DeviceDispatchServiceI.DeviceDispatchServiceI(self._reapter, self._name)
            func = getattr(self._dispatch_service, "data_callback")
            #self.subscribe_data_callback(func)
            if self._center_client is not None:
                self._center_client.subscribe_data_callback(func)
            #self._dispatch_service.
            proxy = self._adapter.add(self._dispatch_service, self._communicator.stringToIdentity(self._name))
            self._dispatch_service.setPushProxy(self._center_client.proxy)
            self._adapter.activate()
            # print("sever_start:time:{0} proxy:{1} connstr:{2} ".format(time.asctime(time.localtime(time.time())),
            # proxy, self.conn_str()))
            logger.info(
                "sever_start:time:{0} proxy:{1} connstr:{2} ".format(time.asctime(time.localtime(time.time())), proxy,
                                                                     self.conn_str()))
            self._communicator.waitForShutdown()
        finally:
            self._reapter.terminate()
            self._reapter.join()

    def stop(self):
        self._center_client.stop()
        self._dispatch_device_to_session_thrd.join()
        self._do_start_server_thrd.join()
        self._set_push_session_thrd.join()
        self._is_start = False


if __name__ == "__main__":
    server = DispatchServer("dispatch", "device_dispatch_service:tcp -h localhost -p 54321", "config_dispatch.server")
    server.start()
    print("conn:{0}".format(server.conn_str()))
    print("out")
    while 1:
        time.sleep()
