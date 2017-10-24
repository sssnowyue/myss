﻿# -*- coding: utf-8 -*-

import collections
import logging
import errno
import socket
import struct
import os
import sys
import weakref
from ss.ioloop import IOLoop
from ss import utils
from . import socks5

# These errnos indicate that a non-blocking operation must be retried
# at a later time.  On most platforms they're the same value, but on
# some they differ.
_ERRNO_WOULDBLOCK = (errno.EWOULDBLOCK, errno.EAGAIN)

if hasattr(errno, "WSAEWOULDBLOCK"):
    _ERRNO_WOULDBLOCK += (errno.WSAEWOULDBLOCK,)  # type: ignore

# These errnos indicate that a connection has been abruptly terminated.
# They should be caught and handled less noisily than other errors.
_ERRNO_CONNRESET = (errno.ECONNRESET, errno.ECONNABORTED, errno.EPIPE,
                    errno.ETIMEDOUT)

if hasattr(errno, "WSAECONNRESET"):
    _ERRNO_CONNRESET += (errno.WSAECONNRESET, errno.WSAECONNABORTED, errno.WSAETIMEDOUT)  # type: ignore

if sys.platform == 'darwin':
    # OSX appears to have a race condition that causes send(2) to return
    # EPROTOTYPE if called while a socket is being torn down:
    # http://erickt.github.io/blog/2014/11/19/adventures-in-debugging-a-potential-osx-kernel-bug/
    # Since the socket is being closed anyway, treat this as an ECONNRESET
    # instead of an unexpected error.
    _ERRNO_CONNRESET += (errno.EPROTOTYPE,)  # type: ignore

# More non-portable errnos:
_ERRNO_INPROGRESS = (errno.EINPROGRESS,)

if hasattr(errno, "WSAEINPROGRESS"):
    _ERRNO_INPROGRESS += (errno.WSAEINPROGRESS,)  # type: ignore

class BaseTCPHandler(object):

    STAGE_INIT, STAGE_CLOSED = (0, -1)
    HDL_POSITIVE, HDL_NEGATIVE, HDL_LISTEN = (0, 1, 2)
    BACKLOG = 1024
    BUF_SIZE = 32 * 1024

    def __init__(self, io_loop, conn, addr, tags, **options):
        """
        @params:
            io_loop,  event loop instance, from ioloop.IOLoop

            conn,     socket.socket instance, or None for `ListenHandler`. 
                      In `ListenHanlder`, it will be initialed in `bind` func.

            addr,     connect addr for this conn, tuple of ip and port. 

            tags,     self-defined tags for this handler. 
                      There are three kinds of tags. `positive-handler-tag` = 0, 
                      `negative-handler-tag` = 1 and `listenhandler-tag` = 2. 
                      The difference between 3 tags is: if `handler.conn` is 
                      generated by `socket.accept` func, handler is `negative`;
                      if `handler.conn` is generated by `socket.socket`, handler
                      is `positive`; if `handler.conn` is generated by `handler.bind`, 
                      handler is `listen`

            options,  config dictionary from config file. readonly!!  
        """
        self.io_loop = io_loop
        self._sock = conn
        self._addr = addr
        self._status = self.STAGE_INIT        # init
        self._events = 0x00
        self._registered = False
        self._last_activity = 0     # 上次活跃时间点, 用于清理长时间没有数据传输的socket连接
        self._read_buf = collections.deque()    # 这个就是peer sock的write缓存, 写sock时需要从里面取出数据
        self._rbuf_size = 0
        self._write_buf = collections.deque()   # 这个其实没太大必要了, 因为这个就是上面的
        self._wbuf_size = 0

        self._started = False
        self._op_hdl_ref = None
        self._config = options
        self._peer_addr = None
        self._tags = tags

    def register(self):
        if self._registered:
            logging.warning("%s already registered!" % self.__class__.__name__)
            return
        if self.closed:
            raise RuntimeError("service %s has been shut down!" % \
            self.__class__.__name__)
        self.io_loop.register(self._sock, IOLoop.READ|IOLoop.ERROR, self)
        self._events = IOLoop.READ|IOLoop.ERROR
        self._registered = True

    def handle_events(self, sock, fd, events):
        raise NotImplementedError()

    def handle_periodic(self):
        pass

    @property
    def closed(self):
        return self._status == self.STAGE_CLOSED

    def on_sock_error(self):
        logging.error("got socket error")
        if self._sock:
            logging.error(utils.get_sock_error(self._sock))
        self.destroy()

    def destroy(self):
        raise NotImplementedError()

    def _append_to_rbuf(self, data, codec=False):
        if codec:
            data = self._codec(data)
        self._read_buf.append(data)
        self._rbuf_size += len(data)

    def _pop_from_rbuf(self, bufsize):
        utils.merge_prefix(self._read_buf, bufsize)
        data = self._read_buf.popleft()
        self._rbuf_size -= len(data)
        return data

    def relate(self, other_handler):
        if self._op_hdl_ref:
            logging.warning("already related!!")
            return
        self._op_hdl_ref = weakref.ref(other_handler)

    def _on_dns_resolved(self, result, error):
        if error:
            logging.error(error)
            self.destroy()
            return
        if result:
            ip = result[1]
            if not ip:
                self.destroy()
                return
            try:
                peer_port = self._peer_addr[1]
                self._status = self.STAGE_DNS_RESOVED
                self._create_peer_socket(ip, peer_port)
            except Exception as e:
                logging.error(e)

    def _create_peer_socket(self, ip, port):
        addrs = socket.getaddrinfo(ip, port, 0, socket.SOCK_STREAM,
                                   socket.SOL_TCP)
        if len(addrs) == 0:
            raise Exception("getaddrinfo failed for %s:%d" % (ip, port))
        af, socktype, proto, canonname, sa = addrs[0]
        sock = socket.socket(af, socktype, proto)
        sock.setblocking(False)
        sock.setsockopt(socket.SOL_TCP, socket.TCP_NODELAY, 1)
        try:
            sock.connect(sa)
        except (OSError, IOError) as e:
            err = utils.errno_from_exception(e)
            if err not in _ERRNO_INPROGRESS and \
                err not in _ERRNO_WOULDBLOCK:
                self.destroy()
                return
            
        peer_handler = self.__class__(self.io_loop, sock, sa, self._dns_resolver, 
                                      self.HDL_POSITIVE, **self._config)
        peer_handler._direct_conn = self._direct_conn
        peer_handler.register()
        self.relate(peer_handler)
        peer_handler.relate(self)

        self._status = self.STAGE_PEER_CONNECTED
        peer_handler._status = self.STAGE_PEER_CONNECTED
        

class BaseMixin(object):
    """"""

    STAGE_INIT = 0 
    STAGET_SOCKS5_NEGO = 1 
    STAGE_SOCKS5_SYN = 2
    STAGE_DNS_RESOVED = 3
    STAGE_PEER_CONNECTED = 4
    STAGE_CLOSED = -1

    ISLOCAL = None

    def __init__(self, dns_resolver):
        assert self.ISLOCAL is not None, \
            "please specify `ISLOCAL`"
        self._dns_resolver = dns_resolver
        self._direct_conn = False

    def _exclusive_host(self, host):
        """
        for local server, it filter the host which in gfw list. 
        for remote server, it filter the user ip which in 
        blacklist.
        @params:
            host, hostname or ip
        @return:
            boolean. return `True` if `host` in blacklist or in gfwlist
        WARNING: need to override
        """
        return False

    def on_recv_nego(self):
        pass

    def on_recv_syn(self):
        pass


class LocalMixin(BaseMixin):
    
    ISLOCAL = 1

    def __init__(self, dns_resolver):
        super(LocalMixin, self).__init__(dns_resolver)
        self._status = self.STAGE_INIT

    def _exclusive_host(self, host):
        
        return True

    def _sshost(self):
        return (self._config["server"], 
            self._config["server_port"])

    def on_recv_nego(self):
        if self._status == self.STAGE_CLOSED:
            logging.warning("read on closed socket!")
            self.destroy()
            return
        data = None
        try:
            data = self._sock.recv(self.BUF_SIZE)
        except (OSError, IOError) as e:
            if utils.errno_from_exception(e) in \
                (errno.ETIMEDOUT, errno.EAGAIN, errno.EWOULDBLOCK):
                return
        if not data:
            self.destroy()
            return
        nego, l = socks5.gen_nego()
        self._write_buf.append(nego)
        self._wbuf_size += l
        self._status = self.STAGET_SOCKS5_NEGO
        return
        
    def on_recv_syn(self):
        if self._status == self.STAGE_CLOSED:
            logging.warning("read on closed socket!")
            self.destroy()
            return
        data = None
        try:
            data = self._sock.recv(self.BUF_SIZE)
        except (OSError, IOError) as e:
            if utils.errno_from_exception(e) in \
                (errno.ETIMEDOUT, errno.EAGAIN, errno.EWOULDBLOCK):
                return
        if not data:
            self.destroy()
            return
        cmd = utils.ord(data[1])
        if cmd == socks5.CMD_UDPFWD:
            logging.debug('UDP associate')
            if self._sock.family == socket.AF_INET6:
                header = b'\x05\x00\x00\x04'
            else:
                header = b'\x05\x00\x00\x01'
            addr, port = self._sock.getsockname()[:2]
            addr_to_send = socket.inet_pton(self._sock.family, addr)
            port_to_send = struct.pack('>H', port)
            data = header + addr_to_send + port_to_send
            self._write_buf.append(data)    # send back ack
            self._wbuf_size += len(data)
            return
        elif cmd == socks5.CMD_CONNECT:
            data = data[3:]
        else:
            logging.error('unknown command %d', cmd)
            self.destroy()
            return
        self._append_to_rbuf(data)
        utils.merge_prefix(self._read_buf, self.BUF_SIZE)    
        if not self._read_buf:
            return
        data = self._read_buf[0]
        header_result = socks5.parse_header(data)
        if not header_result:
            return
        addrtype, remote_addr, remote_port, header_length = header_result
        logging.info("connecting %s:%d from %s:%d" % (\
            (remote_addr, remote_port, ) +  self._addr))
        data = self._pop_from_rbuf(self.BUF_SIZE)
        self._status = self.STAGE_SOCKS5_SYN
        ack, l = socks5.gen_ack()
        self._write_buf.append(ack)    # send back ack
        self._wbuf_size += l
        if self._exclusive_host(remote_addr[0]):    # host in gfwlist
            self._append_to_rbuf(data, codec=True)
            self._peer_addr = self._sshost()        # connect ssserver
        else:
            self._direct_conn = True
            self._peer_addr = (utils.to_str(remote_addr), remote_port)  #直连
        self._dns_resolver.resolve(self._peer_addr[0], 
                                   self._on_dns_resolved)


class RemoteMixin(BaseMixin):
    
    ISLOCAL = 0

    def __init__(self, dns_resolver):
        BaseMixin.__init__(self, dns_resolver)
        self._status = self.STAGET_SOCKS5_NEGO

    def on_recv_syn(self):
        if self._status == self.STAGE_CLOSED:
            logging.warning("read on closed socket!")
            self.destroy()
            return
        data = None
        try:
            data = self._sock.recv(self.BUF_SIZE)
        except (OSError, IOError) as e:
            if utils.errno_from_exception(e) in \
                (errno.ETIMEDOUT, errno.EAGAIN, errno.EWOULDBLOCK):
                return
        if not data:
            self.destroy()
            return
        self._append_to_rbuf(data, codec=True)

        utils.merge_prefix(self._read_buf, self.BUF_SIZE)    
        if not self._read_buf:
            return
        data = self._read_buf[0]
        header_result = socks5.parse_header(data)
        if not header_result:
            return
        addrtype, remote_addr, remote_port, header_length = header_result
        self._pop_from_rbuf(header_length)

        self._status = self.STAGE_SOCKS5_SYN
        logging.info("connecting %s:%d from %s:%d" % (\
            (remote_addr, remote_port, ) +  self._addr))
        self._peer_addr = (utils.to_str(remote_addr), remote_port)
        try:
            self._dns_resolver.resolve(remote_addr, self._on_dns_resolved)
        except Exception as e:
            logging.error(e)
            self.destroy()

