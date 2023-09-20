#!/usr/bin/python3

import asyncio
import importlib
import os
import threading
import time
import tornado
import tornado.web
import tornado.websocket
import traceback
import logging
import random

from inspect import signature

if os.environ.get("ROS_VERSION") == "1":
    import rospy  # ROS1
elif os.environ.get("ROS_VERSION") == "2":
    import rospy2 as rospy  # ROS2
else:
    print(
        "ROS not detected. Please source your ROS environment\n(e.g. 'source /opt/ros/DISTRO/setup.bash')"
    )
    exit(1)

from rosgraph_msgs.msg import Log
from std_msgs.msg import Int8

# SIRV imports
# from sirv_msgs.srv import SimulationConfig

from serialization import ros2dict

# from rosboard.subscribers.dmesg_subscriber import DMesgSubscriber
# from rosboard.subscribers.processes_subscriber import ProcessesSubscriber
# from rosboard.subscribers.system_stats_subscriber import SystemStatsSubscriber
# from rosboard.subscribers.dummy_subscriber import DummySubscriber
# from rosboard.handlers import ROSTornadoBridgeSocketHandler, NoCacheStaticFileHandler
from ROSTornadoBridgeSocketHandler import ROSTornadoBridgeSocketHandler

class ROSTornadoBridge(object):
    def __init__(self, node_name="ROSTornadoBridge"):
        self.__class__.instance = self
        self._node = rospy.init_node(node_name)
        self.port = rospy.get_param("~port", 8001)

        # desired subscriptions of all the websockets connecting to this instance.
        # these remote subs are updated directly by "friend" class ROSTornadoBridgeSocketHandler.
        # this class will read them and create actual ROS subscribers accordingly.
        # dict of topic_name -> set of sockets
        self.remote_subs = {}

        # actual ROS subscribers.
        # dict of topic_name -> ROS Subscriber
        self.local_subs = {}

        # minimum update interval per topic (throttle rate) amang all subscribers to a particular topic.
        # we can throw data away if it arrives faster than this
        # dict of topic_name -> float (interval in seconds)
        self.update_intervals_by_topic = {}

        # last time data arrived for a particular topic
        # dict of topic_name -> float (time in seconds)
        self.last_data_times_by_topic = {}

        if rospy.__name__ == "rospy2":
            # ros2 hack: need to subscribe to at least 1 topic
            # before dynamic subscribing will work later.
            # ros2 docs don't explain why but we need this magic.
            self.sub_rosout = rospy.Subscriber("/rosout", Log, lambda x: x)

        tornado_settings = {
            "debug": True,
        }

        tornado_handlers = [
            (
                r"/ros_tornado_bridge/v1",
                ROSTornadoBridgeSocketHandler,
                {
                    "node": self,
                },
            ),
        ]

        self.event_loop = None
        self.tornado_application = tornado.web.Application(
            tornado_handlers, **tornado_settings
        )
        asyncio.set_event_loop(asyncio.new_event_loop())
        self.event_loop = tornado.ioloop.IOLoop()
        self.tornado_application.listen(self.port)

        # allows tornado to log errors to ROS
        self.logwarn = rospy.logwarn
        self.logerr = rospy.logerr
        self.loginfo = rospy.loginfo

        # tornado event loop. all the web server and web socket stuff happens here
        threading.Thread(target=self.event_loop.start, daemon=True).start()

        # loop to sync remote (websocket) subs with local (ROS) subs
        threading.Thread(target=self.sync_subs_loop, daemon=True).start()

        # loop to keep track of latencies and clock differences for each socket
        threading.Thread(target=self.pingpong_loop, daemon=True).start()

        self.lock = threading.Lock()

        self.random_msg = rospy.Publisher('random_msg', Int8, 10)
        self.timer = self._node.create_timer(0.1, self.random_msg_callback)

        rospy.loginfo("ROS Tornado bridge listening on: %d" % self.port)

    def start(self):
        rospy.spin()

    def get_msg_class(self, msg_type):
        """
        Given a ROS message type specified as a string, e.g.
            "std_msgs/Int32"
        or
            "std_msgs/msg/Int32"
        it imports the message class into Python and returns the class, i.e. the actual std_msgs.msg.Int32

        Returns none if the type is invalid (e.g. if user hasn't bash-sourced the message package).
        """
        try:
            msg_module, dummy, msg_class_name = msg_type.replace("/", ".").rpartition(
                "."
            )
        except ValueError:
            rospy.logerr("invalid type %s" % msg_type)
            return None

        try:
            if not msg_module.endswith(".msg"):
                msg_module = msg_module + ".msg"
            return getattr(importlib.import_module(msg_module), msg_class_name)
        except Exception as e:
            rospy.logerr(str(e))
            return None

    def pingpong_loop(self):
        """
        Loop to send pings to all active sockets every 5 seconds.
        """
        while True:
            time.sleep(5)

            if self.event_loop is None:
                continue
            try:
                self.event_loop.add_callback(ROSTornadoBridgeSocketHandler.send_pings)
            except Exception as e:
                rospy.logwarn(str(e))
                traceback.print_exc()

    def sync_subs_loop(self):
        """
        Periodically calls self.sync_subs(). Intended to be run in a thread.
        """
        while True:
            time.sleep(1)
            self.sync_subs()

    def sync_subs(self):
        """
        Looks at self.remote_subs and makes sure local subscribers exist to match them.
        Also cleans up unused local subscribers for which there are no remote subs interested in them.
        """

        # Acquire lock since either sync_subs_loop or websocket may call this function (from different threads)
        self.lock.acquire()

        try:
            # all topics and their types as strings e.g. {"/foo": "std_msgs/String", "/bar": "std_msgs/Int32"}
            self.all_topics = {}

            for topic_tuple in rospy.get_published_topics():
                topic_name = topic_tuple[0]
                topic_type = topic_tuple[1]
                # self.loginfo(
                # f"::: Setting {topic_name} of {topic_type} type. :::")
                if type(topic_type) is list:
                    topic_type = topic_type[0]  # ROS2
                self.all_topics[topic_name] = topic_type
            self.event_loop.add_callback(
                ROSTornadoBridgeSocketHandler.broadcast,
                [ROSTornadoBridgeSocketHandler.MSG_TOPICS, self.all_topics],
            )

            for topic_name in self.remote_subs:
                if len(self.remote_subs[topic_name]) == 0:
                    continue

                # remote sub special (non-ros) topic: _dmesg
                # handle it separately here
                if topic_name == "_dmesg":
                    if topic_name not in self.local_subs:
                        rospy.loginfo("Subscribing to dmesg [non-ros]")
                        self.local_subs[topic_name] = DMesgSubscriber(self.on_dmesg)
                    continue

                if topic_name == "_system_stats":
                    if topic_name not in self.local_subs:
                        rospy.loginfo("Subscribing to _system_stats [non-ros]")
                        self.local_subs[topic_name] = SystemStatsSubscriber(
                            self.on_system_stats
                        )
                    continue

                if topic_name == "_top":
                    if topic_name not in self.local_subs:
                        rospy.loginfo("Subscribing to _top [non-ros]")
                        self.local_subs[topic_name] = ProcessesSubscriber(self.on_top)
                    continue

                # check if remote sub request is not actually a ROS topic before proceeding
                if topic_name not in self.all_topics:
                    rospy.logwarn("warning: topic %s not found" % topic_name)
                    continue

                # if the local subscriber doesn't exist for the remote sub, create it
                if topic_name not in self.local_subs:
                    topic_type = self.all_topics[topic_name]
                    msg_class = self.get_msg_class(topic_type)

                    if msg_class is None:
                        # invalid message type or custom message package not source-bashed
                        # put a dummy subscriber in to avoid returning to this again.
                        # user needs to re-run rosboard with the custom message files sourced.
                        self.local_subs[topic_name] = DummySubscriber()
                        self.event_loop.add_callback(
                            ROSTornadoBridgeSocketHandler.broadcast,
                            [
                                ROSTornadoBridgeSocketHandler.MSG_MSG,
                                {
                                    "_topic_name": topic_name,  # special non-ros topics start with _
                                    "_topic_type": topic_type,
                                    "_error": "Could not load message type '%s'. Are the .msg files for it source-bashed?"
                                    % topic_type,
                                },
                            ],
                        )
                        continue

                    self.last_data_times_by_topic[topic_name] = 0.0

                    rospy.loginfo("Subscribing to %s" % topic_name)

                    self.local_subs[topic_name] = rospy.Subscriber(
                        topic_name,
                        self.get_msg_class(topic_type),
                        self.on_ros_msg,
                        callback_args=(topic_name, topic_type),
                    )

            # clean up local subscribers for which remote clients have lost interest
            for topic_name in list(self.local_subs.keys()):
                if (
                    topic_name not in self.remote_subs
                    or len(self.remote_subs[topic_name]) == 0
                ):
                    rospy.loginfo("Unsubscribing from %s" % topic_name)
                    self.local_subs[topic_name].unregister()
                    del self.local_subs[topic_name]

        except Exception as e:
            rospy.logwarn(str(e))
            traceback.print_exc()

        self.lock.release()

    def on_system_stats(self, system_stats):
        """
        system stats received. send it off to the client as a "fake" ROS message (which could at some point be a real ROS message)
        """
        if self.event_loop is None:
            return

        msg_dict = {
            "_topic_name": "_system_stats",  # special non-ros topics start with _
            "_topic_type": "rosboard_msgs/msg/SystemStats",
        }

        for key, value in system_stats.items():
            msg_dict[key] = value

        self.event_loop.add_callback(
            ROSTornadoBridgeSocketHandler.broadcast,
            [ROSTornadoBridgeSocketHandler.MSG_MSG, msg_dict],
        )

    def on_top(self, processes):
        """
        processes list received. send it off to the client as a "fake" ROS message (which could at some point be a real ROS message)
        """
        if self.event_loop is None:
            return

        self.event_loop.add_callback(
            ROSTornadoBridgeSocketHandler.broadcast,
            [
                ROSTornadoBridgeSocketHandler.MSG_MSG,
                {
                    "_topic_name": "_top",  # special non-ros topics start with _
                    "_topic_type": "rosboard_msgs/msg/ProcessList",
                    "processes": processes,
                },
            ],
        )

    def on_dmesg(self, text):
        """
        dmesg log received. make it look like a rcl_interfaces/msg/Log and send it off
        """
        if self.event_loop is None:
            return

        self.event_loop.add_callback(
            ROSTornadoBridgeSocketHandler.broadcast,
            [
                ROSTornadoBridgeSocketHandler.MSG_MSG,
                {
                    "_topic_name": "_dmesg",  # special non-ros topics start with _
                    "_topic_type": "rcl_interfaces/msg/Log",
                    "msg": text,
                },
            ],
        )

    def on_ros_msg(self, msg, topic_info):
        """
        ROS messaged received (any topic or type).
        """
        topic_name, topic_type = topic_info
        t = time.time()
        if (
            t - self.last_data_times_by_topic.get(topic_name, 0)
            < self.update_intervals_by_topic[topic_name] - 1e-4
        ):
            return

        if self.event_loop is None:
            return

        # convert ROS message into a dict and get it ready for serialization
        ros_msg_dict = ros2dict(msg)

        # add metadata
        ros_msg_dict["_topic_name"] = topic_name
        ros_msg_dict["_topic_type"] = topic_type
        ros_msg_dict["_time"] = time.time() * 1000

        # log last time we received data on this topic
        self.last_data_times_by_topic[topic_name] = t

        # broadcast it to the listeners that care
        self.event_loop.add_callback(
            ROSTornadoBridgeSocketHandler.broadcast,
            [ROSTornadoBridgeSocketHandler.MSG_MSG, ros_msg_dict],
        )

    def random_msg_callback(self):
        msg = random.randint(-127, 128)
        self.random_msg.publish(msg)
        rospy.loginfo(f"Published {msg}.")
    

    # def send_request_to_start(self, msg):
    #     self.loginfo("::: Requesting to start_simulation :::")
    #     # Preenchendo a estrutura da requisição...
    #     req = SimulationConfig.Request()
    #     req.info.condutor = msg["condutor"]
    #     req.info.idsitio = msg["id_sitio"]
    #     req.info.idmaquina = msg["id_maquina"]
    #     req.info.proposito = msg["proposito"]
    #     req.info.prompt = msg["prompt"]
    #     req.info.luz = msg["luz"]
    #     req.info.farois = msg["farois"]
    #     req.info.radio = msg["radio"]
    #     req.info.volumeradio = msg["volume_radio"]
    #     req.info.clima = msg["tempo"]
    #     req.info.corneblina = msg["cor_neblina"]
    #     req.info.corpoeira = msg["cor_poeira"]
    #     req.info.ordemtestes = msg["ordem_testes"]
    #     req.info.teste_freio = [
    #         msg["alarme_re"],
    #         msg["alarme_re_falha"],
    #         msg["freio_auxiliar"],
    #         msg["freio_estacionamento"],
    #         msg["freio_servico"],
    #         msg["freio_retardador"],
    #         msg["tcs_aeta"],
    #     ]
    #     req.info.ar = msg["ar"]
    #     req.info.tracao = msg["tracao"]
    #     req.info.resistencia = msg["resistencia"]
    #     req.info.rota = msg["rota"]
    #     req.info.carga_id_maquinas = msg["carga_id_maquinas"]
    #     req.info.carga_q_maquinas = msg["carga_q_maquinas"]
    #     # # eventos - indice=evento_id
    #     req.info.entrada = msg["evt_entrada"]  # se for 0 = inabilitado
    #     req.info.pontos1 = msg["evt_pontos1"]
    #     req.info.area_id = msg["evt_area_id"]
    #     # # assistentes - indice=assistente_id
    #     req.info.assistentes = msg["assist_status"]
    #     # # benchmarks - indice=benchmark_id
    #     req.info.benchmark = msg["bench_status"]
    #     req.info.benchmark_meta = msg["bench_meta"]
    #     # # erros - indice=erro_id
    #     req.info.error_id = msg["error_id"]
    #     req.info.error_pontos = msg["error_pontos"]

    #     self.loginfo("::: Creating client start_simulation :::")
    #     start_simulation_client = self._node.create_client(
    #         SimulationConfig, "start_simulation"
    #     )
    #     try:
    #         times = 5
    #         while (
    #             not start_simulation_client.wait_for_service(timeout_sec=1.0)
    #             and times > 0
    #         ):
    #             self.logerr(f"Service start_simulation not available, waiting...")
    #             times -= 1
    #         if times <= 0:
    #             raise Exception("Service start_simulation not available after 5 tries")
    #         self.loginfo("::: Sending... :::")
    #         response = start_simulation_client.call(req)
    #         return response.success
    #     except Exception as exc:
    #         logging.exception(exc)
    #         traceback.format_exc()
    #         return False

    # def send_request_to_stop(self):
    #     self.loginfo("::: Requesting to stop_simulation :::")
    #     self.loginfo("::: Creating stop_simulation client :::")
    #     stop_simulation_client = self._node.create_client(
    #         SimulationConfig, "stop_simulation"
    #     )
    #     try:
    #         times = 5
    #         while (
    #             not stop_simulation_client.wait_for_service(timeout_sec=1.0)
    #             and times > 0
    #         ):
    #             self.logerr(f"Service stop_simulation not available, waiting...")
    #             times -= 1
    #         if times <= 0:
    #             raise Exception("Service start_simulation not available after 5 tries")
    #         self.loginfo("::: Sending... :::")
    #         # TODO Descomente a linha abaixo e comente as duas linhas seguintes
    #         # para habilitar a requisição e retorne response.success
    #         # response = stop_simulation_client.call(SimulationConfig.Request())
    #         return True
    #     except Exception as exc:
    #         logging.exception(exc)
    #         traceback.format_exc()
    #         return False

if __name__ == "__main__":
    from ROSTornadoBridge import ROSTornadoBridge

    node = ROSTornadoBridge()
    node.start()