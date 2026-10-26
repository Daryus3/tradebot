"""
Sunucunun dış (internetteki) IP adresini arka planda izler.

Binance API anahtarında IP kısıtlaması varsa, sunucunun IP'si değiştiği anda emirler -2015 hatasıyla reddedilir.
Bu iş parçacığı IP_CHECK_MINUTES aralıkla api.ipify.org (IPv4) ve api6.ipify.org (IPv6) servislerine sorar;
yeni adresleri ana döngü `poll()` ile alır, kayıtlı adresle karşılaştırır ve değiştiyse Telegram'a alarm gönderir.
Servise ulaşılamazsa (veya sunucunun IPv6'sı yoksa) hiçbir şey yapılmaz.
"""
from __future__ import annotations

import ipaddress
import threading
from typing import Dict, List, Optional, Tuple

import requests

from logger import get_logger

log = get_logger()

SOURCES = (("IPv4", "https://api.ipify.org", 4), ("IPv6", "https://api6.ipify.org", 6))


def fetch_ip(http, url: str, version: int, timeout: float = 5.0) -> Optional[str]:
    """Servisin gördüğü dış IP; ulaşılamazsa veya cevap geçerli bir adres değilse None."""
    try:
        resp = http.get(url, timeout=timeout)
        text = resp.text.strip() if resp.ok else ""
        ip = ipaddress.ip_address(text)
    except (requests.RequestException, ValueError):
        return None
    return str(ip) if ip.version == version else None


class IpMonitor:
    def __init__(self, interval_sec: float = 600, http=None):
        self.interval_sec = interval_sec
        self.http = http or requests.Session()
        self.current: Dict[str, str] = {}
        self._new: List[Tuple[str, str]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self.interval_sec <= 0 or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ip-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def check_once(self) -> Dict[str, str]:
        found = {}
        for family, url, version in SOURCES:
            ip = fetch_ip(self.http, url, version)
            if ip:
                found[family] = ip
        with self._lock:
            for family, ip in found.items():
                if self.current.get(family) != ip:
                    self.current[family] = ip
                    self._new.append((family, ip))
        return found

    def poll(self) -> List[Tuple[str, str]]:
        """Son okumadan bu yana görülen yeni adresler: [(aile, ip)] (ana döngüden çağrılır)."""
        with self._lock:
            out, self._new = self._new, []
        return out

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_once()
            except Exception as exc:  # noqa: BLE001 - izleyici asla ölmesin
                log.debug("IP kontrolü başarısız: %s", exc)
            self._stop.wait(self.interval_sec)
