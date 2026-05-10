"""WorldQuant BRAIN client wrapper.

Wraps the existing exporter at e:/worldquant/file.py (login + paginator) and
adds the methods we need for the alpha factory:
  - submit_simulation(expression, settings) -> simulation_id
  - poll_simulation(simulation_id) -> metrics dict (blocking until done)
  - get_alpha_metrics(alpha_id) -> already-finalized alpha metrics
  - get_self_correlation(alpha_id) -> max/min corr against the user's library

This module deliberately does *not* re-implement auth or pagination. Import
the existing helpers if available; otherwise reproduce the minimum needed.

NOTE: BRAIN's exact submission endpoint and payload shape may evolve. The
wrapper isolates that surface so the rest of the factory is stable.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Optional

import requests

API = "https://api.worldquantbrain.com"


@dataclass
class BrainSettings:
    instrument_type: str = "EQUITY"
    region: str = "USA"
    universe: str = "TOP3000"
    delay: int = 1
    decay: int = 0
    neutralization: str = "NONE"
    truncation: float = 0.08
    pasteurization: str = "ON"
    nan_handling: str = "OFF"

    def to_payload(self) -> dict:
        return {
            "instrumentType": self.instrument_type,
            "region": self.region,
            "universe": self.universe,
            "delay": self.delay,
            "decay": self.decay,
            "neutralization": self.neutralization,
            "truncation": self.truncation,
            "pasteurization": self.pasteurization,
            "nanHandling": self.nan_handling,
        }


class BrainAuthError(RuntimeError):
    pass


class BrainSubmitError(RuntimeError):
    pass


class BrainClient:
    """Synchronous client. For high-throughput use, swap requests for aiohttp
    in a follow-up; this MVP keeps the dependency surface minimal.
    """

    def __init__(
        self,
        email: str | None = None,
        password: str | None = None,
        api_base: str = API,
        rate_limit_seconds: float = 15.0,
    ):
        self.api_base = api_base.rstrip("/")
        self.session = requests.Session()
        self._last_submit = 0.0
        self.rate_limit_seconds = rate_limit_seconds
        self._email = email or os.environ.get("BRAIN_EMAIL")
        self._password = password or os.environ.get("BRAIN_PASSWORD")
        if self._email and self._password:
            self.login(self._email, self._password)

    # ---------- auth ----------

    def login(self, email: str, password: str) -> None:
        self.session.auth = (email, password)
        r = self.session.post(f"{self.api_base}/authentication")
        if r.status_code not in (200, 201):
            raise BrainAuthError(f"login failed: {r.status_code} {r.text[:200]}")
        self._email = email

    # ---------- simulation submit + poll ----------

    def submit_simulation(
        self,
        expression: str,
        settings: BrainSettings | None = None,
    ) -> str:
        """Submit a simulation. Returns the simulation/alpha id."""
        settings = settings or BrainSettings()
        # rate-limit
        wait = self.rate_limit_seconds - (time.time() - self._last_submit)
        if wait > 0:
            time.sleep(wait)
        payload = {
            "type": "REGULAR",
            "settings": settings.to_payload(),
            "regular": expression,
        }
        r = self.session.post(f"{self.api_base}/simulations", json=payload)
        self._last_submit = time.time()
        if r.status_code == 429:
            time.sleep(20)
            return self.submit_simulation(expression, settings)
        if r.status_code not in (200, 201):
            raise BrainSubmitError(
                f"submit failed: {r.status_code} {r.text[:300]}"
            )
        data = r.json()
        # the response shape varies; look for common id keys
        for key in ("id", "simulationId", "alphaId"):
            if key in data:
                return str(data[key])
        # location header fallback
        loc = r.headers.get("Location", "")
        if loc:
            return loc.rstrip("/").rsplit("/", 1)[-1]
        raise BrainSubmitError(f"could not extract id from response: {data}")

    def poll_simulation(
        self, simulation_id: str, *, timeout_seconds: int = 600,
        poll_interval: float = 5.0,
    ) -> dict[str, Any]:
        """Block until the simulation completes; return the result dict."""
        start = time.time()
        while time.time() - start < timeout_seconds:
            r = self.session.get(f"{self.api_base}/simulations/{simulation_id}")
            if r.status_code == 200:
                payload = r.json()
                status = payload.get("status", "").upper()
                if status in ("COMPLETE", "FINISHED", "DONE"):
                    return payload
                if status in ("ERROR", "FAILED"):
                    raise BrainSubmitError(
                        f"simulation {simulation_id} failed: {payload}"
                    )
            time.sleep(poll_interval)
        raise TimeoutError(f"simulation {simulation_id} timed out after {timeout_seconds}s")

    # ---------- alpha metrics ----------

    def get_alpha_metrics(self, alpha_id: str) -> dict[str, Any]:
        """Fetch finalized alpha metrics by id."""
        r = self.session.get(f"{self.api_base}/alphas/{alpha_id}")
        if r.status_code != 200:
            raise BrainSubmitError(
                f"get_alpha_metrics({alpha_id}) failed: {r.status_code}"
            )
        return r.json()

    def get_self_correlation(
        self, alpha_id: str
    ) -> tuple[Optional[float], Optional[float]]:
        """Return (max, min) self-correlation of the alpha vs the user's library."""
        # endpoint commonly /alphas/<id>/correlations/self
        r = self.session.get(
            f"{self.api_base}/alphas/{alpha_id}/correlations/self"
        )
        if r.status_code != 200:
            return (None, None)
        payload = r.json()
        # payload shape: {"records": [{"correlation": x, "alphaId": y}, ...]}
        records = payload.get("records") or payload.get("results") or []
        if not records:
            return (None, None)
        cors = [r.get("correlation") for r in records if r.get("correlation") is not None]
        if not cors:
            return (None, None)
        return (max(cors), min(cors))


def from_env() -> BrainClient:
    """Construct a BrainClient using BRAIN_EMAIL / BRAIN_PASSWORD env vars."""
    email = os.environ.get("BRAIN_EMAIL")
    password = os.environ.get("BRAIN_PASSWORD")
    if not email or not password:
        raise BrainAuthError(
            "BRAIN_EMAIL and BRAIN_PASSWORD must be set in environment"
        )
    return BrainClient(email=email, password=password)
