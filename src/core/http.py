"""Polite HTTP client.

Combines four production concerns into one Session:
1. Token-bucket rate limiting (per-host)
2. Retries with exponential backoff (Tenacity)
3. robots.txt enforcement (one-time check at startup)
4. Custom User-Agent so the operator is identifiable to the site

The same client is shared by the Algolia layer and the product-page layer; a
host-keyed dict of token buckets keeps the two tiers from starving each other.
"""

from __future__ import annotations

import threading
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.core.config import RateLimitsConfig, RetryConfig, SiteConfig
from src.core.logger import get_logger

log = get_logger(__name__)


class RobotsBlocked(RuntimeError):
    pass


class RetryableHTTPError(RuntimeError):
    pass


class TokenBucket:
    def __init__(self, rate_per_sec: float, burst: int) -> None:
        self.rate = rate_per_sec
        self.capacity = burst
        self.tokens = float(burst)
        self.last = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self) -> None:
        with self.lock:
            now = time.monotonic()
            elapsed = now - self.last
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self.last = now
            if self.tokens < 1:
                wait = (1 - self.tokens) / self.rate
                time.sleep(wait)
                self.tokens = 0.0
            else:
                self.tokens -= 1.0


class HttpClient:
    """Thin wrapper around requests.Session with rate limiting + retries."""

    def __init__(
        self,
        site: SiteConfig,
        rate_limits: RateLimitsConfig,
        retry_cfg: RetryConfig,
    ) -> None:
        self.site = site
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": site.user_agent})
        self.retry_cfg = retry_cfg

        self.buckets = {
            "algolia": TokenBucket(rate_limits.algolia.rate_per_sec, rate_limits.algolia.burst),
            "safco": TokenBucket(rate_limits.safco.rate_per_sec, rate_limits.safco.burst),
        }

        self._rp: RobotFileParser | None = None

    # ── compliance ────────────────────────────────────────────────────────

    def load_robots(self) -> None:
        rp = RobotFileParser()
        rp.set_url(self.site.robots_txt_url)
        try:
            rp.read()
            self._rp = rp
            log.info("robots_loaded", url=self.site.robots_txt_url)
        except Exception as e:  # noqa: BLE001 — robots is best-effort
            log.warning("robots_load_failed", error=str(e))
            self._rp = None

    def can_fetch(self, url: str) -> bool:
        # Algolia is a third-party host; only check robots for the safco origin
        if "safcodental.com" not in url:
            return True
        if self._rp is None:
            return True
        return self._rp.can_fetch(self.session.headers["User-Agent"], url)

    # ── core request ─────────────────────────────────────────────────────

    def _bucket_for(self, url: str) -> TokenBucket:
        host = urlparse(url).hostname or ""
        if "algolia.net" in host:
            return self.buckets["algolia"]
        return self.buckets["safco"]

    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        if not self.can_fetch(url):
            raise RobotsBlocked(f"robots.txt disallows {url}")

        self._bucket_for(url).acquire()

        retryer = retry(
            stop=stop_after_attempt(self.retry_cfg.max_attempts),
            wait=wait_exponential(
                multiplier=self.retry_cfg.initial_backoff_sec,
                max=self.retry_cfg.max_backoff_sec,
            ),
            retry=retry_if_exception_type(
                (requests.ConnectionError, requests.Timeout, RetryableHTTPError)
            ),
            before_sleep=before_sleep_log(log._logger, 30) if hasattr(log, "_logger") else None,
            reraise=True,
        )

        @retryer
        def _do() -> requests.Response:
            kwargs.setdefault("timeout", 30)
            r = self.session.request(method, url, **kwargs)
            # 5xx + 429 are retryable; 4xx (other) is not
            if r.status_code in (429,) or 500 <= r.status_code < 600:
                raise RetryableHTTPError(f"{r.status_code} from {url}")
            return r

        return _do()

    def get(self, url: str, **kwargs) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> requests.Response:
        return self.request("POST", url, **kwargs)
