import asyncio
import threading

from PySide6.QtCore import QObject, Signal
import pypalmsens as ps

from src.channel_status import channel_status_snapshot
from src.measurement_runner import TemperatureStepCoordinator, measurement_runner


class palmsens_connection_service(QObject):
    connected = Signal()
    connection_failed = Signal(str)
    disconnected = Signal()
    status_received = Signal(object, object)
    measurement_progress = Signal(int, object)
    measurement_finished = Signal(int, object)
    measurement_failed = Signal(int, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread = None
        self._loop = None
        self._stop_requested = threading.Event()
        self._instruments = []
        self._managers = {}
        self._current_range_options = {}
        self._runners = {}
        self._pending_aborts = set()
        self._runner_lock = threading.Lock()
        self._temperature_coordinator = None
        self._synchronize_temperature_steps = False

    @property
    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, instruments):
        if self.is_running:
            raise RuntimeError("PalmSens connection service is already running.")

        self._instruments = list(instruments)
        self._stop_requested = threading.Event()
        self._managers.clear()
        self._current_range_options.clear()
        with self._runner_lock:
            self._runners.clear()
            self._pending_aborts.clear()
        self._temperature_coordinator = None
        self._synchronize_temperature_steps = False

        self._thread = threading.Thread(
            target=self._run,
            name="PalmSensConnectionService",
        )
        self._thread.start()

    def stop(self, *, wait=False):
        thread = self._thread
        if thread is None:
            return True

        self._stop_requested.set()
        if not wait or not thread.is_alive():
            return True

        thread.join(timeout=10.0)
        stopped = not thread.is_alive()
        if stopped and thread is self._thread:
            self._thread = None
        return stopped

    def start_measurement(self, run_id, instrument, method, temperature_settings=None):
        loop = self._loop
        if loop is None or not loop.is_running():
            self.measurement_failed.emit(run_id, "PalmSens connection is not ready.")
            return

        if temperature_settings is not None and temperature_settings.sync_channels:
            self._synchronize_temperature_steps = True

        asyncio.run_coroutine_threadsafe(
            self._run_measurement(run_id, instrument, method, temperature_settings),
            loop,
        )

    def abort_measurement(self, run_id):
        with self._runner_lock:
            runner = self._runners.get(run_id)
            if runner is None:
                self._pending_aborts.add(run_id)
                return
        runner.abort()

    def current_range_options(self, instrument):
        options = self._current_range_options.get(id(instrument), {})
        return {field_key: tuple(values) for field_key, values in options.items()}

    def _run(self):
        try:
            asyncio.run(self._serve())
        except Exception as exc:
            self.connection_failed.emit(str(exc))
        finally:
            self.disconnected.emit()

    async def _serve(self):
        self._loop = asyncio.get_running_loop()
        try:
            await self._connect_channels()
            self._temperature_coordinator = TemperatureStepCoordinator(
                set(self._managers)
            )
            if not self._stop_requested.is_set():
                self.connected.emit()
            while not self._stop_requested.is_set():
                await asyncio.sleep(0.1)
        finally:
            await self._disconnect_channels()
            self._temperature_coordinator = None
            self._loop = None

    async def _connect_channels(self):
        for instrument in self._instruments:
            if self._stop_requested.is_set():
                return
            if getattr(instrument, "interface", None) == "mock":
                continue

            try:
                manager = await ps.connect_async(instrument=instrument)
                self._managers[id(instrument)] = manager
                range_options = {}
                try:
                    range_options["current_range"] = tuple(
                        manager.supported_current_ranges()
                    )
                except Exception:
                    pass
                try:
                    range_options["applied_current_range"] = tuple(
                        manager.supported_applied_current_ranges()
                    )
                except Exception:
                    pass
                self._current_range_options[id(instrument)] = range_options
                manager.register_status_callback(
                    self._status_callback_for(instrument)
                )
            except Exception as exc:
                channel = getattr(instrument, "channel", -1)
                channel_name = (
                    f"channel {channel}"
                    if channel > 0
                    else getattr(instrument, "name", "channel")
                )
                raise RuntimeError(
                    f"Could not connect {channel_name}: {exc}"
                ) from exc

    async def _disconnect_channels(self):
        for manager in reversed(list(self._managers.values())):
            try:
                manager.unregister_status_callback()
            except Exception:
                pass
            try:
                await manager.disconnect()
            except Exception:
                pass
        self._managers.clear()
        self._current_range_options.clear()

    def _status_callback_for(self, instrument):
        def handle_status(status):
            snapshot = channel_status_snapshot.from_pypalmsens(status)
            self.status_received.emit(instrument, snapshot)

        return handle_status

    async def _run_measurement(
        self,
        run_id,
        instrument,
        method,
        temperature_settings,
    ):
        manager = self._managers.get(id(instrument))
        if manager is None:
            self.measurement_failed.emit(run_id, "This channel is not connected.")
            return

        runner = measurement_runner(
            instrument,
            method,
            temperature_settings=temperature_settings,
            temperature_coordinator=self._temperature_coordinator,
            synchronize_temperature_steps=self._synchronize_temperature_steps,
        )
        runner.progress.connect(
            lambda data, run_id=run_id: self.measurement_progress.emit(run_id, data)
        )

        with self._runner_lock:
            if any(active.instrument is instrument for active in self._runners.values()):
                self.measurement_failed.emit(run_id, "This channel is already measuring.")
                return
            self._runners[run_id] = runner
            abort_requested = run_id in self._pending_aborts
            self._pending_aborts.discard(run_id)

        if abort_requested:
            runner.abort()

        try:
            measurement = await runner.measure_with_manager(manager)
        except Exception as exc:
            self.measurement_failed.emit(run_id, str(exc))
        else:
            self.measurement_finished.emit(run_id, measurement)
        finally:
            if self._temperature_coordinator is not None:
                self._temperature_coordinator.withdraw(id(instrument))
            with self._runner_lock:
                self._runners.pop(run_id, None)
                self._pending_aborts.discard(run_id)
                if not self._runners:
                    self._synchronize_temperature_steps = False
