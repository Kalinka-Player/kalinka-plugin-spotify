"""Restart a failed Connect receiver without retaining its playback session."""

import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class Supervisor:
    def __init__(
        self,
        create_service,
        *,
        retry_delay=1,
        max_retry_delay=30,
        max_signin_retry_delay=300,
        stable_after=60,
    ):
        self.create_service = create_service
        self.retry_delay = retry_delay
        self.max_retry_delay = max_retry_delay
        # Spotify can refuse sign-in for hours; poll it gently until it returns.
        self.max_signin_retry_delay = max_signin_retry_delay
        self.stable_after = stable_after
        self.session = None
        self.task = None
        self.closed = False
        self.retrying = False
        self.retry_status = "Starting Spotify Connect"
        self.startup_error = None

    @property
    def capture(self):
        return self.session.capture if self.session and not self.retrying else None

    @property
    def status(self):
        if self.retrying or self.session is None:
            return self.startup_error or self.retry_status
        return self.session.status

    @property
    def error(self):
        if self.startup_error:
            return self.startup_error
        return self.session.error if self.session and not self.retrying else None

    @property
    def output_error(self):
        return self.session.output_error if self.session and not self.retrying else None

    def start(self):
        self.task = asyncio.create_task(self.run())

    async def run(self):
        delay = self.retry_delay
        try:
            while not self.closed:
                started = time.monotonic()
                self.session = self.create_service()
                self.retrying = False
                self.session.start()
                try:
                    # Session watchdogs cancel their own task. Shield keeps
                    # that distinct from cancellation of the supervisor.
                    await asyncio.shield(self.session.task)
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise
                await self.session.stop()
                if self.closed or not self.session.restartable:
                    break
                if time.monotonic() - started >= self.stable_after:
                    delay = self.retry_delay
                self.retrying = True
                signin = self.session.signin_failed
                self.retry_status = (
                    f"{self.session.error}; retrying in {delay:g} seconds"
                    if signin
                    else f"Spotify Connect restarting in {delay:g} seconds"
                )
                logger.warning("%s", self.retry_status)
                await asyncio.sleep(delay)
                ceiling = (
                    self.max_signin_retry_delay if signin else self.max_retry_delay
                )
                delay = min(delay * 2, ceiling)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Avoid logging exception messages or locals from external inputs.
            self.startup_error = (
                "Spotify receiver could not be started; check its configuration."
            )
            logger.error("%s (%s)", self.startup_error, type(exc).__name__)
        finally:
            self.closed = True
            if self.session:
                await self.session.stop()

    async def stop(self):
        self.closed = True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.session:
            await self.session.stop()
