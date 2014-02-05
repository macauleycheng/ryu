from operator import attrgetter
from oslo.config import cfg
from ryu.base import app_manager
from ryu.controller import handler
from ryu.controller import dpset
from ryu.controller import ofp_event
from ryu.ofproto import ofproto_parser
from ryu.ofproto import ofproto_v1_2
from ryu.ofproto import ofproto_v1_2_parser
from ryu.ofproto import ofproto_v1_3
from ryu.ofproto import ofproto_v1_3_parser
from ryu.lib import hub
from ryu.lib import apgw
from ryu.lib import rpc
from ryu.lib.packet import packet
from ryu.lib.packet import ethernet
from ryu.lib.packet import icmp
from ryu.lib.packet import ipv4
from ryu.lib.packet import vlan


_ = type('', (apgw.StructuredMessage,), {})
_.COMPONENT_NAME = 'ofwire'


class RPCError(Exception):
    pass


class NoRPCResponse(Exception):
    def __init__(self, dpid, xid, msgid):
        self.dpid = dpid
        self.xid = xid
        self.msgid = msgid


class PendingRPC(Exception):
    pass


class Peer(object):
    def __init__(self, queue):
        super(Peer, self).__init__()
        self._queue = queue
        self.wait_for_ofp_resepnse = {}

    def _handle_rpc_request(self, data):
        self._queue.put((self, rpc.MessageType.REQUEST, data))

    def _handle_rpc_notify(self, data):
        self._queue.put((self, rpc.MessageType.NOTIFY, data))


class RpcOFPManager(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_2.OFP_VERSION, ofproto_v1_3.OFP_VERSION]
    LOGGER_NAME = 'ofwire'
    _CONTEXTS = {
        'dpset': dpset.DPSet,
    }

    def __init__(self, *args, **kwargs):
        super(RpcOFPManager, self).__init__(*args, **kwargs)
        self.dpset = kwargs['dpset']
        self._peers = []
        self.traceroute_source = {}
        self.monitored_ports = {}
        self.monitored_flows = {}
        self.pending_rpc_requests = []
        self._rpc_events = hub.Queue(128)
        # per 30 secs by default
        self.port_monitor_interval = 30
        hub.spawn(self._peer_accept_thread)
        hub.spawn(self._port_status_thread)
        hub.spawn(self._rpc_message_thread)
        apgw.update_syslog_format()

    def _rpc_message_thread(self):
        while True:
            (peer, _type, data) = self._rpc_events.get()
            error = None
            result = None
            try:
                msgid, target_method, params = data
                if _type == rpc.MessageType.REQUEST:
                    if target_method == "ofp":
                        result = self._handle_ofprotocol(msgid, params)
                    elif target_method == "monitor_port":
                        result = self._monitor_port(msgid, params)
                    else:
                        error = 'Unknown method %s' % (target_method)
                elif _type == rpc.MessageType.NOTIFY:
                    if target_method == 'traceroute':
                        result = self._register_traceroute(msgid, params)
                    else:
                        error = 'Unknown method %s' % (target_method)
            except RPCError as e:
                error = str(e)
            except PendingRPC as e:
                # we handle the RPC request after a datapath joins.
                self.pending_rpc_requests.append((peer, data))
                continue
            except NoRPCResponse as e:
                # we'll send RPC sesponse after we get a response from
                # datapath.
                d = peer.wait_for_ofp_resepnse.setdefault(e.dpid, {})
                d[e.xid] = e.msgid
                continue

            peer._endpoint.send_response(msgid, error=error, result=result)

    def _peer_loop_thread(self, peer):
        peer._endpoint.serve()
        # the peer connection is closed
        self._peers.remove(peer)

    def peer_accept_handler(self, new_sock, addr):
        peer = Peer(self._rpc_events)
        table = {
            rpc.MessageType.REQUEST: peer._handle_rpc_request,
            rpc.MessageType.NOTIFY: peer._handle_rpc_notify,
            }
        peer._endpoint = rpc.EndPoint(new_sock, disp_table=table)
        self._peers.append(peer)
        hub.spawn(self._peer_loop_thread, peer)

    def _peer_accept_thread(self):
        server = hub.StreamServer(('', 50001),
                                  self.peer_accept_handler)
        server.serve_forever()

    def _port_status_thread(self):
        while True:
            for k, dp in self.dpset.get_all():
                try:
                    ofmsg = dp.ofproto_parser.OFPPortStatsRequest(datapath=dp)
                    dp.send_msg(ofmsg)
                except:
                    # ignore the error due to dead datapath
                    pass
            hub.sleep(self.port_monitor_interval)

    def _send_waited_rpc_response(self, msg):
        for peer in self._peers:
            if not msg.datapath.id in peer.wait_for_ofp_resepnse:
                continue
            if msg.xid in peer.wait_for_ofp_resepnse[msg.datapath.id]:
                msgid = peer.wait_for_ofp_resepnse[msg.datapath.id][msg.xid]
                peer._endpoint.send_response(msgid, error=None,
                                             result=msg.to_jsondict())
                del peer.wait_for_ofp_resepnse[msg.datapath.id][msg.xid]
                return

    def compare_key(self, k1, k2):
        k1 = eval(k1)
        k2 = eval(k2)
        l1 = k1['OFPMatch']['oxm_fields']
        l2 = k2['OFPMatch']['oxm_fields']
        return sorted(l1) == sorted(l2)

    def format_key(self, match_json):
        del match_json['OFPMatch']['length']
        for t in match_json['OFPMatch']['oxm_fields']:
            tlv = t['OXMTlv']
            if tlv['field'] in ['ipv4_dst', 'ipv4_src']:
                if tlv['mask'] == '255.255.255.255':
                    tlv['mask'] = None
        return str(match_json)

    @handler.set_ev_cls(dpset.EventDP)
    def _handler_datapath(self, ev):
        if ev.enter:
            dp = ev.dp
            parser = dp.ofproto_parser
            ofp = dp.ofproto
            if ofp.OFP_VERSION == ofproto_v1_2.OFP_VERSION:
                m = parser.OFPSetConfig(dp,
                                        ofp.OFPC_INVALID_TTL_TO_CONTROLLER,
                                        ofp.OFPCML_MAX)
            elif ofp.OFP_VERSION == ofproto_v1_3.OFP_VERSION:
                packet_in_mask = ofp.OFPR_ACTION | ofp.OFPR_INVALID_TTL
                port_status_mask = (ofp.OFPPR_ADD | ofp.OFPPR_DELETE |
                                    ofp.OFPPR_MODIFY)
                m = parser.OFPSetAsync(dp, [packet_in_mask, 0],
                                       [port_status_mask, 0],
                                       [0, 0])
            dp.send_msg(m)

            log_msg = {"event": "dp connected", "dpid": ev.dp.id}
            notify_param = {'secure_channel_state': 'Up'}
            for p in self.pending_rpc_requests:
                (peer, data) = p
                self._rpc_events.put((peer, rpc.MessageType.REQUEST, data))
        else:
            log_msg = {"event": "dp disconnected"}
            notify_param = {'secure_channel_state': 'Down'}
            for peer in self._peers:
                if ev.dp.id in peer.wait_for_ofp_resepnse:
                    del peer.wait_for_ofp_resepnse[ev.dp.id]

        self.logger.info(_(log_msg))
        for peer in self._peers:
            peer._endpoint.send_notification("state", [notify_param])

    @handler.set_ev_cls(ofp_event.EventOFPErrorMsg,
                        handler.MAIN_DISPATCHER)
    def _error_msg_handler(self, ev):
        self.logger.info(_(ev.msg.to_jsondict()))

    @handler.set_ev_cls(ofp_event.EventOFPBarrierReply,
                        handler.MAIN_DISPATCHER)
    def _barrier_reply_handler(self, ev):
        self._send_waited_rpc_response(ev.msg)

    @handler.set_ev_cls(ofp_event.EventOFPFlowStatsReply,
                        handler.MAIN_DISPATCHER)
    def _flow_stats_reply_handler(self, ev):
        msg = ev.msg
        for body in msg.body:
            key = self.format_key(body.match.to_jsondict())
            contexts = None
            for k in self.monitored_flows.keys():
                if self.compare_key(k, key):
                    contexts = self.monitored_flows[k]
                    break
            if contexts is not None:
                stats = {'byte_count': body.byte_count,
                         'packet_count': body.packet_count,
                         'match': body.match.to_jsondict(),
                         'table_id': body.table_id}
                stats.update(contexts)
                self.logger.info(_(msg=stats, log_type='stats'))

    @handler.set_ev_cls(ofp_event.EventOFPPortStatsReply,
                        handler.MAIN_DISPATCHER)
    def _port_stats_reply_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        for stat in sorted(msg.body, key=attrgetter('port_no')):
            try:
                port = self.dpset.get_port(dp.id, stat.port_no)
            except:
                continue
            if port.name in self.monitored_ports:
                stats = {'physical_port_no': port.name}
                stats.update(stat.to_jsondict()['OFPPortStats'])
                stats.update(self.monitored_ports[port.name])
                self.logger.info(_(msg=stats, log_type='stats'))

    @handler.set_ev_cls(ofp_event.EventOFPStatsReply,
                        handler.MAIN_DISPATCHER)
    def _stats_reply_handler(self, ev):
        msg = ev.msg
        self._send_waited_rpc_response(msg)

        if msg.type == ofproto_v1_2.OFPST_FLOW:
            self._flow_stats_reply_handler(ev)
        elif msg.type == ofproto_v1_2.OFPST_PORT:
            self._port_stats_reply_handler(ev)

    @handler.set_ev_cls(ofp_event.EventOFPPacketIn)
    def _packet_in_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        self.logger.info(_({"event": "packet_in", "reason": msg.reason}))
        if dp.ofproto.OFPR_INVALID_TTL != msg.reason:
            return

        if not 'in_port' in msg.match:
            return
        in_port = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        if not pkt.get_protocol(ipv4.ipv4):
            return

        o_vlan = pkt.get_protocol(vlan.vlan)
        if o_vlan is None:
            return
        vlan_p = vlan.vlan(vid=o_vlan.vid)

        o_eth = pkt.get_protocol(ethernet.ethernet)
        eth = ethernet.ethernet(o_eth.src, o_eth.dst, o_eth.ethertype)
        o_ip = pkt.get_protocol(ipv4.ipv4)
        # needs to set src properly for either side (vlan or mpls)
        # ip = ipv4.ipv4(src=ip_lib.ipv4_to_bin(V1_GS_IP), dst=o_ip.src,
        #                proto=1)
        try:
            src_ip = self.traceroute_source[o_vlan.vid]['ip']
            in_port = self.traceroute_source[o_vlan.vid]['port']
        except:
            self.logger.info(_({"event": "traceroute error",
                                "reason": "can't find ip", "vid": o_vlan.vid}))
            return
        ip = ipv4.ipv4(src=src_ip, dst=o_ip.src, proto=1)
        ip_offset = 14 + 4
        # ether + vlan headers
        data = msg.data[ip_offset:ip_offset +
                        (o_ip.header_length * 4 + 8)]
        ic = icmp.icmp(icmp.ICMP_TIME_EXCEEDED, 0, 0,
                       icmp.TimeExceeded(data_len=len(data), data=data))

        p = packet.Packet(protocols=[eth, vlan_p, ip, ic])
        p.serialize()
        actions = [dp.ofproto_parser.OFPActionOutput(dp.ofproto.OFPP_TABLE, 0)]
        dp.send_packet_out(in_port=in_port, actions=actions, data=p.data)

    @handler.set_ev_cls(ofp_event.EventOFPPortStatus)
    def _port_status_handler(self, ev):
        if hasattr(ev, 'msg'):
            msg = ev.msg

            reason = msg.reason
            datapath = msg.datapath
            port = msg.desc
            ofproto = datapath.ofproto
            self.logger.info(_({"event": "port status change",
                                "reason": reason,
                                "port_no": port.port_no, "state": port.state},
                               log_type='states'))
            # For now just port modifications are reported
            if reason == ofproto.OFPPR_MODIFY:
                params = {'port_no': port.port_no, 'port_state': port.state}
                for peer in self._peers:
                    peer._endpoint.send_notification("port_status", [params])

    def _flow_stats_loop(self, dp, table_id, match, interval, key):
        while True:
            if not key in self.monitored_flows:
                break
            msg = dp.ofproto_parser.OFPFlowStatsRequest(datapath=dp,
                                                        table_id=table_id,
                                                        match=match)
            dp.send_msg(msg)
            hub.sleep(interval)

    def _handle_ofprotocol(self, msgid, params):
        try:
            param_dict = params[0]
        except:
            raise RPCError('parameters are missing')

        send_response = True

        dp = None
        if 'dpid' in param_dict:
            dp = self.dpset.get(int(param_dict['dpid']))
            param_dict.pop('dpid')
        else:
            # use the first datapath
            for k, v in self.dpset.get_all():
                dp = v
                break

        if dp is None:
            self.logger.info(_({"event": "no datapath, queued",
                                "msg": str(param_dict)}))
            raise PendingRPC()

        contexts = None
        ofmsg = None
        # default interval
        interval = 60
        for k, v in param_dict.items():
            if k == 'ofmsg':
                try:
                    ofmsg = ofproto_parser.ofp_msg_from_jsondict(dp, v)
                except:
                    raise RPCError('parameters are invalid, %s' %
                                   (str(param_dict)))
            elif k == 'interval':
                interval = int(v)
            elif k == 'contexts':
                contexts = v
        if ofmsg is None:
            raise RPCError('"ofmsg" parameter is invalid, %s' %
                           (str(param_dict)))
        if contexts is not None and not isinstance(contexts, dict):
            raise RPCError('"contexts" must be dictionary, %s' %
                           (str(param_dict)))
        if contexts is not None and interval == 0:
            raise RPCError('"interval" must be non zero with "contexts", %s' %
                           (str(param_dict)))

        dp.set_xid(ofmsg)
        ofmsg.serialize()
        if dp.ofproto.OFP_VERSION == ofproto_v1_2.OFP_VERSION:
            msg_types = (dp.ofproto.OFPT_STATS_REQUEST,
                         dp.ofproto.OFPT_BARRIER_REQUEST)
        else:
            msg_types = (dp.ofproto.OFPT_MULTIPART_REQUEST,
                         dp.ofproto.OFPT_BARRIER_REQUEST)

        if ofmsg.msg_type in msg_types:
            dp.send_msg(ofmsg)
            raise NoRPCResponse(dpid=dp.id, xid=ofmsg.xid, msgid=msgid)

        result = {'xid': ofmsg.xid}
        if ofmsg.msg_type is dp.ofproto.OFPT_FLOW_MOD:
            if contexts is not None:
                key = self.format_key(ofmsg.match.to_jsondict())
                if ofmsg.command is dp.ofproto.OFPFC_ADD:
                    if key in self.monitored_flows:
                        raise RPCError('the existing flow, %s' % (str(key)))

                    self.monitored_flows[key] = contexts
                    hub.spawn(self._flow_stats_loop,
                              dp, ofmsg.table_id, ofmsg.match,
                              interval, key)

                elif ofmsg.command in (dp.ofproto.OFPFC_DELETE,
                                       dp.ofproto.OFPFC_DELETE_STRICT):
                    try:
                        del self.monitored_flows[key]
                    except:
                        raise RPCError('unknown key, %s' % (str(key)))
        else:
            raise RPCError('unknown of message, %s' % (str(param_dict)))

        dp.send_msg(ofmsg)
        return result

    def _register_traceroute(self, msgid, params):
        try:
            param_dict = params[0]
        except:
            raise RPCError('parameters are missing')
        try:
            self.traceroute_source[param_dict['vlan']] = {
                'ip': param_dict['ip'],
                'port': param_dict['port']
                }
        except Exception as e:
            raise RPCError('parameters are invalid, %s' % (str(param_dict)))

        self.logger.info(_({'event': 'register traceroute source',
                            'vlan': param_dict['vlan'],
                            'ip': param_dict['ip'],
                            'port': param_dict['port']}))
        return {}

    def _monitor_port(self, msgid, params):
        try:
            param_dict = params[0]
        except:
            raise RPCError('parameters are missing')
        name = None
        contexts = None
        for k, v in param_dict.items():
            if k == 'physical_port_no':
                name = v
            elif k == 'contexts':
                contexts = v
            elif k == 'interval':
                self.port_monitor_interval = v
            else:
                raise RPCError('unknown parameters, %s' % k)

        if contexts is None:
            raise RPCError('"contexts" parameter is necessary')
        if not isinstance(contexts, dict):
            raise RPCError('"contexts" parameter must be dictionary')
        if name is None:
            raise RPCError('"physical_port_no" parameter is necessary')
        self.monitored_ports[name] = contexts
        return {}