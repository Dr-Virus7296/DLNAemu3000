"""SSDP advertiser for UPnP MediaServer.

Работает рядом с minidlna: SO_REUSEADDR/PORT, ответы на M-SEARCH
отправляются с отдельного сокета, NOTIFY чаще.
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
time = __import__("time")
from typing import Optional

log = logging.getLogger("dlna_emu.ssdp")

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900


class SSDPServer(threading.Thread):
    def __init__(
        self,
        uuid: str,
        location: str,
        server_name: str = "DLNA-emu",
        interval: int = 20,
        announce_ip: Optional[str] = None,
    ):
        super().__init__(daemon=True, name="ssdp")
        self.uuid = uuid
        self.location = location
        self.server_name = server_name
        self.interval = interval
        self.announce_ip = announce_ip  # LAN IP роутера
        self._stop = threading.Event()
        self._sock: Optional[socket.socket] = None
        self.ok = False
        self.last_error = ""

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass

    def _make_udp(self) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # SO_REUSEPORT есть не везде (в Windows его нет вообще).
        # Берём через getattr, чтобы не падать с AttributeError.
        reuse_port = getattr(socket, "SO_REUSEPORT", None)
        if reuse_port is not None:
            try:
                s.setsockopt(socket.SOL_SOCKET, reuse_port, 1)
            except OSError:
                pass
        return s

    def _set_mcast_iface(self, sock: socket.socket) -> None:
        """Привязать multicast к LAN-интерфейсу (важно на роутере)."""
        if not self.announce_ip:
            return
        try:
            sock.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_MULTICAST_IF,
                socket.inet_aton(self.announce_ip),
            )
        except OSError as e:
            log.debug("IP_MULTICAST_IF: %s", e)
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
        except OSError:
            pass
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        except OSError:
            pass

    def run(self) -> None:
        sock = self._make_udp()
        bind_addr = self.announce_ip if self.announce_ip else ""
        try:
            # Сначала на LAN IP, потом на все интерфейсы
            try:
                if bind_addr:
                    sock.bind((bind_addr, SSDP_PORT))
                    log.info("SSDP bound to %s:%s", bind_addr, SSDP_PORT)
                else:
                    sock.bind(("", SSDP_PORT))
            except OSError:
                sock.close()
                sock = self._make_udp()
                sock.bind(("", SSDP_PORT))
                log.info("SSDP bound to 0.0.0.0:%s", SSDP_PORT)
        except OSError as e:
            self.last_error = f"bind :1900 failed: {e}"
            log.error(
                "SSDP %s — ресивер может не видеть сервер. "
                "minidlna уже слушает 1900? Пробуем только NOTIFY.",
                self.last_error,
            )
            # Без bind всё равно шлём NOTIFY (часть клиентов подхватит)
            self._sock = None
            self.ok = False
            self._notify_loop_only()
            return

        # Вступить в multicast-группу, желательно на LAN IP
        try:
            if self.announce_ip:
                mreq = struct.pack(
                    "4s4s",
                    socket.inet_aton(SSDP_ADDR),
                    socket.inet_aton(self.announce_ip),
                )
            else:
                mreq = struct.pack("=4sl", socket.inet_aton(SSDP_ADDR), socket.INADDR_ANY)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError as e:
            log.warning("SSDP multicast join failed: %s (пробуем INADDR_ANY)", e)
            try:
                mreq = struct.pack("=4sl", socket.inet_aton(SSDP_ADDR), socket.INADDR_ANY)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            except OSError as e2:
                self.last_error = f"membership: {e2}"
                log.error("SSDP %s", self.last_error)

        self._set_mcast_iface(sock)
        sock.settimeout(1.0)
        self._sock = sock
        self.ok = True
        log.info(
            "SSDP OK LOCATION=%s iface=%s",
            self.location,
            self.announce_ip or "any",
        )

        # Сразу несколько NOTIFY — ресивер быстрее увидит
        for _ in range(3):
            self._notify()
            time.sleep(0.3)

        last_notify = time.time()
        while not self._stop.is_set():
            now = time.time()
            if now - last_notify >= self.interval:
                self._notify()
                last_notify = now
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                text = data.decode("utf-8", errors="replace")
            except Exception:
                continue
            if "M-SEARCH" in text[:16].upper() or text.startswith("M-SEARCH"):
                self._handle_search(text, addr)

        self._byebye()
        try:
            sock.close()
        except Exception:
            pass

    def _notify_loop_only(self) -> None:
        """Если :1900 занят без REUSE — только периодический NOTIFY."""
        log.warning("SSDP notify-only mode (no M-SEARCH replies)")
        for _ in range(5):
            self._notify()
            time.sleep(0.4)
        while not self._stop.is_set():
            self._notify()
            self._stop.wait(self.interval)
        self._byebye()

    def _usn(self, st: str) -> str:
        if st == "upnp:rootdevice":
            return f"uuid:{self.uuid}::upnp:rootdevice"
        if st.startswith("uuid:"):
            return st
        return f"uuid:{self.uuid}::{st}"

    def _send_udp(self, data: bytes, addr) -> None:
        try:
            s = self._make_udp()
            self._set_mcast_iface(s)
            if self.announce_ip:
                try:
                    s.bind((self.announce_ip, 0))
                except OSError:
                    pass
            s.sendto(data, addr)
            s.close()
        except Exception as e:
            log.debug("udp send %s: %s", addr, e)

    def _notify_st(self, nt: str) -> None:
        msg = (
            "NOTIFY * HTTP/1.1\r\n"
            f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
            "CACHE-CONTROL: max-age=1800\r\n"
            f"LOCATION: {self.location}\r\n"
            f"NT: {nt}\r\n"
            "NTS: ssdp:alive\r\n"
            f"SERVER: Linux/3.0 UPnP/1.0 {self.server_name}/0.1\r\n"
            f"USN: {self._usn(nt)}\r\n"
            "\r\n"
        )
        self._send_udp(msg.encode("utf-8"), (SSDP_ADDR, SSDP_PORT))

    def _notify(self) -> None:
        for nt in (
            "upnp:rootdevice",
            f"uuid:{self.uuid}",
            "urn:schemas-upnp-org:device:MediaServer:1",
            "urn:schemas-upnp-org:service:ContentDirectory:1",
            "urn:schemas-upnp-org:service:ConnectionManager:1",
        ):
            self._notify_st(nt)

    def _byebye(self) -> None:
        for nt in (
            "upnp:rootdevice",
            "urn:schemas-upnp-org:device:MediaServer:1",
        ):
            msg = (
                "NOTIFY * HTTP/1.1\r\n"
                f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
                f"NT: {nt}\r\n"
                "NTS: ssdp:byebye\r\n"
                f"USN: {self._usn(nt)}\r\n"
                "\r\n"
            )
            self._send_udp(msg.encode("utf-8"), (SSDP_ADDR, SSDP_PORT))

    def _handle_search(self, text: str, addr) -> None:
        st = "ssdp:all"
        for line in text.split("\r\n"):
            if line.upper().startswith("ST:"):
                st = line.split(":", 1)[1].strip()
                break

        log.info("M-SEARCH from %s ST=%s", addr, st)

        media = "urn:schemas-upnp-org:device:MediaServer"
        ok = (
            st in (
                "ssdp:all",
                "upnp:rootdevice",
                f"uuid:{self.uuid}",
                "urn:schemas-upnp-org:device:MediaServer:1",
                "urn:schemas-upnp-org:service:ContentDirectory:1",
                "urn:schemas-upnp-org:service:ConnectionManager:1",
            )
            or st.startswith(media)
        )
        if not ok:
            return

        if st == "ssdp:all":
            for r_st in (
                "upnp:rootdevice",
                "urn:schemas-upnp-org:device:MediaServer:1",
                "urn:schemas-upnp-org:service:ContentDirectory:1",
            ):
                self._search_response(r_st, addr)
        else:
            self._search_response(st, addr)

    def _search_response(self, st: str, addr) -> None:
        msg = (
            "HTTP/1.1 200 OK\r\n"
            "CACHE-CONTROL: max-age=1800\r\n"
            "EXT:\r\n"
            f"LOCATION: {self.location}\r\n"
            f"SERVER: Linux/3.0 UPnP/1.0 {self.server_name}/0.1\r\n"
            f"ST: {st}\r\n"
            f"USN: {self._usn(st)}\r\n"
            "Content-Length: 0\r\n"
            "\r\n"
        )
        # Ответ напрямую искателю (не через multicast-сокет)
        self._send_udp(msg.encode("utf-8"), addr)
        log.info("SSDP reply ST=%s -> %s LOCATION=%s", st, addr, self.location)