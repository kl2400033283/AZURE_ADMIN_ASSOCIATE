"""
Azure Chatty Application
------------------------
A lightweight Python + FastAPI application designed for Azure cloud architecture
demonstrations and hackathons. It generates controlled, asynchronous outbound HTTP
traffic to test and demonstrate outbound connectivity, SNAT port behavior,
and Azure NAT Gateway scalability.
"""

import asyncio
import os
import socket
import time
from collections import deque
from typing import Dict, List, Optional
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, HttpUrl

# Initialize FastAPI application
app = FastAPI(
    title="Azure Chatty Application",
    description="Demonstrates outbound HTTP traffic generation and Azure NAT Gateway SNAT scalability",
    version="1.0.0"
)

# Template engine setup
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# System & Safety Limits
SERVER_HOSTNAME = socket.gethostname()
DEFAULT_TARGET_URL = "https://httpbin.org/get"

MAX_ALLOWED_REQUESTS = int(os.getenv("MAX_ALLOWED_REQUESTS", "10000"))
MAX_ALLOWED_RPS = int(os.getenv("MAX_ALLOWED_RPS", "500"))
MAX_ALLOWED_WORKERS = int(os.getenv("MAX_ALLOWED_WORKERS", "50"))
MAX_TEST_TIMEOUT_SECONDS = int(os.getenv("MAX_TEST_TIMEOUT_SECONDS", "300"))


class StartTestRequest(BaseModel):
    target_url: str = Field(
        default=DEFAULT_TARGET_URL,
        description="Outbound destination URL"
    )
    total_requests: int = Field(
        default=1000,
        ge=1,
        le=MAX_ALLOWED_REQUESTS,
        description=f"Total outbound requests (max {MAX_ALLOWED_REQUESTS})"
    )
    requests_per_second: int = Field(
        default=50,
        ge=1,
        le=MAX_ALLOWED_RPS,
        description=f"Outbound rate in req/sec (max {MAX_ALLOWED_RPS})"
    )
    concurrent_workers: int = Field(
        default=10,
        ge=1,
        le=MAX_ALLOWED_WORKERS,
        description=f"Simultaneous worker tasks (max {MAX_ALLOWED_WORKERS})"
    )
    reuse_connections: bool = Field(
        default=True,
        description="Whether to reuse HTTP connections (keep-alive) or force fresh TCP sockets"
    )


class TrafficManager:
    """Manages background asynchronous outbound HTTP traffic generation and statistics."""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.is_running: bool = False
        self.status: str = "idle"  # idle, running, stopping, completed, error
        self.target_url: str = DEFAULT_TARGET_URL
        self.target_total_requests: int = 1000
        self.target_rps: int = 50
        self.concurrent_workers: int = 10
        self.reuse_connections: bool = True

        # Telemetry metrics
        self.total_requests: int = 0
        self.successful_requests: int = 0
        self.failed_requests: int = 0
        self.total_response_time_ms: float = 0.0
        self.min_response_time_ms: Optional[float] = None
        self.max_response_time_ms: Optional[float] = None

        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None
        self.active_workers: int = 0

        # Rolling window for calculating real-time RPS (stores timestamps of completed requests)
        self._completion_timestamps: deque = deque(maxlen=2000)
        # Ring buffer for recent activity logs (shown on the web dashboard)
        self.recent_logs: deque = deque(maxlen=50)
        # Summary of errors by reason / status code
        self.error_summary: Dict[str, int] = {}

        # Asynchronous control primitives
        self.stop_event: asyncio.Event = asyncio.Event()
        self.main_task: Optional[asyncio.Task] = None

    def reset_stats(self):
        """Resets all telemetry counters."""
        self.total_requests = 0
        self.successful_requests = 0
        self.failed_requests = 0
        self.total_response_time_ms = 0.0
        self.min_response_time_ms = None
        self.max_response_time_ms = None
        self.start_time = None
        self.end_time = None
        self.active_workers = 0
        self._completion_timestamps.clear()
        self.recent_logs.clear()
        self.error_summary.clear()
        self.status = "idle"

    def get_stats_dict(self) -> dict:
        """Returns the current statistics dictionary matching the required API."""
        now = time.time()

        # Calculate average response time
        if self.total_requests > 0:
            avg_resp_time = round(self.total_response_time_ms / self.total_requests, 2)
        else:
            avg_resp_time = 0.0

        # Calculate current request rate (RPS)
        if self.is_running:
            # Measure requests completed in the last 2 seconds for a responsive real-time rate
            cutoff = now - 2.0
            recent_count = sum(1 for ts in self._completion_timestamps if ts >= cutoff)
            current_rps = round(recent_count / 2.0, 1)
        elif self.start_time and self.end_time and (self.end_time > self.start_time):
            elapsed = self.end_time - self.start_time
            current_rps = round(self.total_requests / elapsed, 1) if elapsed > 0 else 0.0
        else:
            current_rps = 0.0

        elapsed_seconds = 0.0
        if self.start_time:
            end_point = self.end_time if self.end_time else now
            elapsed_seconds = round(end_point - self.start_time, 1)

        return {
            # Required keys per specification
            "total_requests": self.total_requests,
            "successful_requests": self.successful_requests,
            "failed_requests": self.failed_requests,
            "requests_per_second": current_rps,
            "average_response_time_ms": avg_resp_time,
            # Enhanced keys for dashboard and debugging
            "hostname": SERVER_HOSTNAME,
            "is_running": self.is_running,
            "status": self.status,
            "target_url": self.target_url,
            "target_total_requests": self.target_total_requests,
            "target_rps": self.target_rps,
            "concurrent_workers": self.concurrent_workers,
            "active_workers": self.active_workers,
            "min_response_time_ms": round(self.min_response_time_ms, 2) if self.min_response_time_ms is not None else 0.0,
            "max_response_time_ms": round(self.max_response_time_ms, 2) if self.max_response_time_ms is not None else 0.0,
            "elapsed_seconds": elapsed_seconds,
            "reuse_connections": self.reuse_connections,
            "error_summary": dict(self.error_summary),
            "recent_logs": list(self.recent_logs)
        }

    async def start(self, config: StartTestRequest):
        """Initiates outbound traffic generation in a background asyncio task."""
        async with self.lock:
            if self.is_running:
                raise HTTPException(status_code=409, detail="A traffic generation test is already running.")

            self.reset_stats()
            self.target_url = config.target_url
            self.target_total_requests = config.total_requests
            self.target_rps = config.requests_per_second
            self.concurrent_workers = config.concurrent_workers
            self.reuse_connections = config.reuse_connections

            self.is_running = True
            self.status = "running"
            self.start_time = time.time()
            self.stop_event.clear()

            self.main_task = asyncio.create_task(self._run_traffic_test())

    async def stop(self):
        """Signals running workers to halt gracefully."""
        async with self.lock:
            if not self.is_running:
                return
            self.status = "stopping"
            self.stop_event.set()

        # Wait for main task to wrap up or cancel it
        if self.main_task and not self.main_task.done():
            self.main_task.cancel()
            try:
                await asyncio.wait_for(self.main_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

        self.is_running = False
        self.status = "stopped"

    async def _run_traffic_test(self):
        """Core asynchronous traffic generation coordinator."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=self.concurrent_workers * 2)
        rate_interval = 1.0 / self.target_rps

        # Configure HTTP client connection pool
        # If reuse_connections is True, we use keep-alive pooling.
        # If False, each request establishes a brand-new TCP socket (accelerates SNAT exhaustion).
        limits = httpx.Limits(
            max_keepalive_connections=self.concurrent_workers if self.reuse_connections else 0,
            max_connections=self.concurrent_workers * 2,
            keepalive_expiry=5.0 if self.reuse_connections else 0.0
        )
        transport = httpx.AsyncHTTPTransport(
            limits=limits,
            retries=0  # Fail fast to clearly report connection errors/timeouts
        )

        async with httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
            headers={"User-Agent": "Azure-Chatty-App/1.0", "Connection": "keep-alive" if self.reuse_connections else "close"}
        ) as client:

            # Producer task: meters out request permits up to target_total_requests at target_rps
            async def producer():
                try:
                    for req_index in range(1, self.target_total_requests + 1):
                        if self.stop_event.is_set():
                            break
                        # Put with periodic stop checking to prevent blocking if queue is full
                        while not self.stop_event.is_set():
                            try:
                                await asyncio.wait_for(queue.put(req_index), timeout=0.2)
                                break
                            except asyncio.TimeoutError:
                                continue

                        if self.stop_event.is_set():
                            break
                        await asyncio.sleep(rate_interval)

                    # Send sentinel None to signal workers to finish
                    for _ in range(self.concurrent_workers):
                        while not self.stop_event.is_set():
                            try:
                                await asyncio.wait_for(queue.put(None), timeout=0.2)
                                break
                            except asyncio.TimeoutError:
                                continue
                except asyncio.CancelledError:
                    pass

            # Worker task: executes outbound requests
            async def worker(worker_id: int):
                try:
                    while not self.stop_event.is_set():
                        if self.total_requests >= self.target_total_requests:
                            break

                        try:
                            req_index = await asyncio.wait_for(queue.get(), timeout=0.3)
                        except asyncio.TimeoutError:
                            continue

                        if req_index is None:
                            queue.task_done()
                            break

                        self.active_workers += 1
                        req_start = time.perf_counter()
                        timestamp_str = time.strftime("%H:%M:%S") + f".{int((time.time() % 1) * 1000):03d}"
                        status_code = None
                        is_success = False
                        error_text = None

                        try:
                            # Perform real outbound HTTP request
                            response = await client.get(self.target_url)
                            latency_ms = (time.perf_counter() - req_start) * 1000.0
                            status_code = response.status_code
                            is_success = 200 <= status_code < 400
                            if not is_success:
                                error_text = f"HTTP {status_code}"
                        except httpx.ConnectTimeout:
                            latency_ms = (time.perf_counter() - req_start) * 1000.0
                            error_text = "ConnectTimeout (Possible SNAT Drop)"
                        except httpx.ReadTimeout:
                            latency_ms = (time.perf_counter() - req_start) * 1000.0
                            error_text = "ReadTimeout"
                        except httpx.ConnectError as e:
                            latency_ms = (time.perf_counter() - req_start) * 1000.0
                            error_text = f"ConnectError: {type(e).__name__}"
                        except Exception as e:
                            latency_ms = (time.perf_counter() - req_start) * 1000.0
                            error_text = f"Error: {type(e).__name__}"
                        finally:
                            self.active_workers -= 1
                            queue.task_done()

                        # Record metrics
                        self.total_requests += 1
                        self.total_response_time_ms += latency_ms
                        self._completion_timestamps.append(time.time())

                        if self.min_response_time_ms is None or latency_ms < self.min_response_time_ms:
                            self.min_response_time_ms = latency_ms
                        if self.max_response_time_ms is None or latency_ms > self.max_response_time_ms:
                            self.max_response_time_ms = latency_ms

                        if is_success:
                            self.successful_requests += 1
                        else:
                            self.failed_requests += 1
                            err_key = error_text or f"HTTP {status_code}"
                            self.error_summary[err_key] = self.error_summary.get(err_key, 0) + 1

                        # Log entry
                        self.recent_logs.append({
                            "id": req_index,
                            "time": timestamp_str,
                            "worker": worker_id,
                            "status": status_code if status_code else "ERR",
                            "latency_ms": round(latency_ms, 1),
                            "success": is_success,
                            "message": error_text if error_text else f"{status_code} OK"
                        })

                        if self.total_requests >= self.target_total_requests:
                            break
                except asyncio.CancelledError:
                    pass

            # Spawn producer and pool of worker tasks
            prod_task = asyncio.create_task(producer())
            worker_tasks = [asyncio.create_task(worker(i + 1)) for i in range(self.concurrent_workers)]

            try:
                # Enforce overall test timeout safety limit
                await asyncio.wait_for(
                    asyncio.gather(prod_task, *worker_tasks, return_exceptions=True),
                    timeout=MAX_TEST_TIMEOUT_SECONDS
                )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                if not self.stop_event.is_set():
                    self.error_summary["Safety Timeout Exceeded"] = self.error_summary.get("Safety Timeout Exceeded", 0) + 1
            finally:
                # Clean up all background subtasks
                if not prod_task.done():
                    prod_task.cancel()
                for wt in worker_tasks:
                    if not wt.done():
                        wt.cancel()
                await asyncio.gather(prod_task, *worker_tasks, return_exceptions=True)

                self.end_time = time.time()
                self.is_running = False
                self.active_workers = 0
                if self.stop_event.is_set():
                    self.status = "stopped"
                else:
                    self.status = "completed"


# Global traffic manager instance
traffic_manager = TrafficManager()


# -----------------------------------------------------------------------------
# REST & Web Endpoints
# -----------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def get_dashboard(request: Request):
    """Renders the Azure Chatty Application web dashboard."""
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "hostname": SERVER_HOSTNAME,
            "default_target_url": DEFAULT_TARGET_URL,
            "max_requests": MAX_ALLOWED_REQUESTS,
            "max_rps": MAX_ALLOWED_RPS,
            "max_workers": MAX_ALLOWED_WORKERS,
            "stats": traffic_manager.get_stats_dict()
        }
    )


@app.get("/health")
async def health_check():
    """
    Health Endpoint required by Azure Load Balancer and health probes.
    Returns: {"status": "healthy"}
    """
    return {"status": "healthy"}


@app.get("/stats")
async def get_statistics():
    """
    Statistics Endpoint.
    Returns real-time metrics including total_requests, successful_requests,
    failed_requests, requests_per_second, and average_response_time_ms.
    """
    return traffic_manager.get_stats_dict()


@app.post("/start")
async def start_test(payload: StartTestRequest):
    """Starts a new outbound traffic generation test."""
    if not (payload.target_url.startswith("http://") or payload.target_url.startswith("https://")):
        raise HTTPException(status_code=400, detail="Target URL must begin with http:// or https://")

    await traffic_manager.start(payload)
    return {"message": "Traffic generation started successfully", "config": payload.model_dump()}


@app.post("/stop")
async def stop_test():
    """Gracefully stops the active traffic generation test."""
    await traffic_manager.stop()
    return {"message": "Stop signal sent to background workers", "status": traffic_manager.status}


@app.post("/reset")
async def reset_statistics():
    """Resets all metrics counters back to zero."""
    if traffic_manager.is_running:
        raise HTTPException(status_code=400, detail="Cannot reset stats while test is running. Stop the test first.")
    traffic_manager.reset_stats()
    return {"message": "Telemetry counters reset successfully", "stats": traffic_manager.get_stats_dict()}


if __name__ == "__main__":
    import uvicorn
    # Default port 8000 as required
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
