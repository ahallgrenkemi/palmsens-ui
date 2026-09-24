
import asyncio
from dataclasses import replace
import threading
import time

from PySide6.QtCore import QObject, Signal
import pypalmsens as ps

from aurora_method_builder.methods import AuroraStepwiseMethod
from src.measurement_data import (
    AuroraStepCompleted,
    LiveMeasurementStarted,
    LogicalMeasurementRun,
    MeasurementSegment,
    TemperatureSample,
)
from src.temperature_chamber.temperature_controller import TemperatureController, TemperatureProgress


_DEFAULT_TEMPERATURE_OCV_INTERVAL_S = 1.0
# PalmSens methods need a finite duration. Target-reached steps abort this OCV early.
_TARGET_REACHED_OCV_LIMIT_S = 24 * 60 * 60


class TemperatureStepCoordinator:
    """Release synchronized temperature steps when every active channel arrives."""

    def __init__(self, instrument_ids: set[int]):
        self.instrument_ids = instrument_ids
        self.arrivals: dict[int, set[int]] = {}
        self.events: dict[int, asyncio.Event] = {}
        self.withdrawn: set[int] = set()

    async def wait_for_all(self, execution_index: int, instrument_id: int) -> None:
        if instrument_id not in self.instrument_ids:
            return

        event = self.events.setdefault(execution_index, asyncio.Event())
        arrivals = self.arrivals.setdefault(execution_index, set())
        arrivals.add(instrument_id)
        if arrivals >= self.instrument_ids - self.withdrawn:
            event.set()
        await event.wait()

    def withdraw(self, instrument_id: int) -> None:
        self.withdrawn.add(instrument_id)
        for execution_index, arrivals in self.arrivals.items():
            if arrivals >= self.instrument_ids - self.withdrawn:
                self.events[execution_index].set()


class measurement_runner(QObject):
    progress = Signal(object)

    def __init__(
        self,
        instrument,
        method,
        temperature_settings=None,
        temperature_coordinator=None,
        synchronize_temperature_steps=False,
    ):
        super().__init__()
        self.instrument = instrument
        self.method = method
        self.temperature_settings = temperature_settings
        self.temperature_coordinator = temperature_coordinator
        self.synchronize_temperature_steps = synchronize_temperature_steps
        self.manager = None
        self.loop = None
        self.abort_requested = False
        self._state_lock = threading.Lock()

    async def measure_with_manager(self, manager):
        try:
            with self._state_lock:
                self.manager = manager
                self.loop = asyncio.get_running_loop()
                abort_requested = self.abort_requested

            if isinstance(self.method, AuroraStepwiseMethod):
                return await self._measure_aurora_stepwise(manager, self.method)

            manager.validate_method(self.method)

            if abort_requested:
                return LogicalMeasurementRun(type(self.method).__name__)

            def on_data(data):
                self.progress.emit(data)

            temperature_controller = self._connect_temperature_controller()
            try:
                self.progress.emit(LiveMeasurementStarted())
                measurement, temperature_samples = await self._measure_palmsens(
                    manager,
                    self.method,
                    on_data,
                    temperature_controller,
                )
            finally:
                if temperature_controller is not None:
                    temperature_controller.close()

            if temperature_controller is None:
                return measurement

            title = getattr(measurement, "title", None) or type(self.method).__name__
            segment = MeasurementSegment(
                index=1,
                label=title,
                source=measurement,
                temperature_samples=temperature_samples,
            )
            return LogicalMeasurementRun(title, [segment])
        finally:
            with self._state_lock:
                self.manager = None
                self.loop = None

    async def _measure_aurora_stepwise(self, manager, stepwise_method: AuroraStepwiseMethod):
        actions = stepwise_method.render_actions()
        if not actions:
            raise RuntimeError("Aurora package did not produce any executable steps.")

        run = LogicalMeasurementRun(stepwise_method.name)
        run_start = time.monotonic()

        if any(action.is_temperature for action in actions) and self.temperature_settings is None:
            raise RuntimeError("This Aurora method contains temperature steps, but the chamber is not enabled.")
        temperature_controller = self._connect_temperature_controller()

        def on_data(data):
            self.progress.emit(data)

        try:
            for action in actions:
                if self._abort_requested():
                    break

                if action.is_temperature:
                    method = self._temperature_ocv_method(
                        stepwise_method,
                        action,
                        sync_channels=(
                            self.synchronize_temperature_steps
                        ),
                    )
                elif action.is_palmsens and action.methodscript is not None:
                    method = ps.MethodScript(script=action.methodscript)
                else:
                    continue

                manager.validate_method(method)
                segment_offset_s = time.monotonic() - run_start
                segment = MeasurementSegment(
                    index=len(run.segments) + 1,
                    label=action.label,
                    source=None,
                    elapsed_offset_s=segment_offset_s,
                    source_step_index=action.source_step_index,
                    step_type=action.step_type,
                    execution_index=action.execution_index,
                )
                self.progress.emit(
                    LiveMeasurementStarted(
                        run_title=run.title,
                        segment=segment,
                    )
                )
                try:
                    if action.is_temperature:
                        measurement, temperature_samples = (
                            await self._execute_temperature_action(
                                manager,
                                method,
                                on_data,
                                temperature_controller,
                                action,
                            )
                        )
                    else:
                        measurement, temperature_samples = await self._measure_palmsens(
                            manager,
                            method,
                            on_data,
                            temperature_controller,
                        )
                except Exception:
                    if not self._abort_requested():
                        raise
                    break
                segment = replace(
                    segment,
                    source=measurement,
                    temperature_samples=temperature_samples,
                )
                run.add_segment(segment)
                self.progress.emit(AuroraStepCompleted(segment))

                if self._abort_requested():
                    break
        finally:
            if temperature_controller is not None:
                if self._abort_requested() and self.temperature_settings.stop_on_abort:
                    temperature_controller.stop()
                temperature_controller.close()

        if not run.segments and not self._abort_requested():
            raise RuntimeError("Aurora step-wise execution completed without measurement data.")
        return run

    def _connect_temperature_controller(self) -> TemperatureController | None:
        if self.temperature_settings is None:
            return None

        controller = TemperatureController(self.temperature_settings)
        controller.connect()
        return controller

    async def _measure_palmsens(
        self,
        manager,
        method,
        callback,
        temperature_controller: TemperatureController | None,
        temperature_status_callback=None,
        stop_when_temperature_status=None,
    ):
        # Pathway 1: temperature chamber is not enabled
        if temperature_controller is None:
            measurement = await manager.measure(method, callback=callback)
            return measurement, ()

        # Pathway 2: temperature chamber is enabled --> poll temperature and setpoint and store
        samples: list[TemperatureSample] = []
        stop_polling = threading.Event()
        temperature_complete = threading.Event()
        measurement_started_at = time.monotonic()
        polling_task = asyncio.create_task(
            asyncio.to_thread(
                self._poll_temperature,
                temperature_controller,
                stop_polling,
                measurement_started_at,
                samples,
                temperature_status_callback,
                stop_when_temperature_status,
                temperature_complete,
            )
        )

        completion_task = None
        try:
            measurement_task = asyncio.create_task(manager.measure(method, callback=callback))
            if stop_when_temperature_status is None:
                measurement = await measurement_task
            else:
                completion_task = asyncio.create_task(
                    asyncio.to_thread(temperature_complete.wait)
                )
                await asyncio.wait(
                    (measurement_task, completion_task),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if temperature_complete.is_set() and not measurement_task.done():
                    await manager.abort()
                measurement = await measurement_task
        finally:
            stop_polling.set()
            temperature_complete.set()
            await polling_task
            if completion_task is not None:
                await completion_task

        return measurement, tuple(samples)

    @staticmethod
    def _poll_temperature(
        controller: TemperatureController,
        stop_polling: threading.Event,
        measurement_started_at: float,
        samples: list[TemperatureSample],
        status_callback,
        stop_when_status,
        completion_event: threading.Event,
    ):
        while not stop_polling.is_set():
            status = controller.poll_status()
            if stop_polling.is_set():
                return
            if status is not None:
                samples.append(
                    TemperatureSample(
                        elapsed_s=time.monotonic() - measurement_started_at,
                        temperature_c=status.temperature_c,
                        setpoint_c=status.setpoint_c,
                    )
                )
                if status_callback is not None:
                    status_callback(status)
                if stop_when_status is not None and stop_when_status(status):
                    completion_event.set()
                    return

    @staticmethod
    def _temperature_ocv_method(stepwise_method, action, sync_channels=False):
        record = stepwise_method.protocol_json.get("record", {})
        try:
            interval_s = float(
                record.get("time_s") or _DEFAULT_TEMPERATURE_OCV_INTERVAL_S
            )
        except (TypeError, ValueError):
            interval_s = _DEFAULT_TEMPERATURE_OCV_INTERVAL_S
        if interval_s <= 0:
            interval_s = _DEFAULT_TEMPERATURE_OCV_INTERVAL_S

        wait_s = max(float(action.wait_after_s or 0.0), interval_s)
        run_time_s = (
            _TARGET_REACHED_OCV_LIMIT_S
            if sync_channels
            else wait_s
            if action.wait_starts_immediately
            else _TARGET_REACHED_OCV_LIMIT_S
        )
        return ps.OpenCircuitPotentiometry(
            interval_time=interval_s,
            run_time=run_time_s,
        )

    async def _execute_temperature_action(
        self,
        manager,
        method,
        callback,
        temperature_controller,
        action,
    ):
        if temperature_controller is None:
            raise RuntimeError("Temperature chamber is not configured.")

        target_c = action.target_temperature_c
        if target_c is None:
            raise RuntimeError("Temperature step is missing a target temperature.")

        wait_s = action.wait_after_s or 0.0
        synchronized = (
            self.temperature_settings is not None
            and self.synchronize_temperature_steps
            and self.temperature_coordinator is not None
        )

        if synchronized:
            return await self._execute_synchronized_temperature_action(
                manager,
                method,
                callback,
                temperature_controller,
                action,
                wait_s,
            )

        self.progress.emit(
            TemperatureProgress(
                target_c=target_c,
                temperature_c=None,
                setpoint_c=None,
                wait_elapsed_s=0.0,
                message=f"Setting chamber to {target_c:.2f} C",
            )
        )

        if action.ramp_rate_c_per_min is not None:
            temperature_controller.set_ramp_rate(action.ramp_rate_c_per_min)
        temperature_controller.start()
        temperature_controller.set_target(target_c)

        started_at = time.monotonic()
        wait_started_at = started_at if action.wait_starts_immediately else None
        latest_status = None
        temperature_step_complete = False

        def handle_status(status):
            nonlocal latest_status, wait_started_at, temperature_step_complete
            latest_status = status
            now = time.monotonic()

            if not action.wait_starts_immediately:
                error_c = abs(status.temperature_c - target_c)
                if error_c <= temperature_controller.settings.tolerance_c:
                    wait_started_at = wait_started_at or now
                else:
                    wait_started_at = None

            wait_elapsed_s = (
                now - wait_started_at if wait_started_at is not None else 0.0
            )
            temperature_step_complete = (
                wait_started_at is not None and wait_elapsed_s >= wait_s
            )
            self.progress.emit(
                TemperatureProgress(
                    target_c=target_c,
                    temperature_c=status.temperature_c,
                    setpoint_c=status.setpoint_c,
                    wait_elapsed_s=wait_elapsed_s,
                    message=temperature_controller.progress_message(
                        status,
                        target_c,
                        wait_elapsed_s,
                        wait_s,
                        action.wait_starts_immediately,
                    ),
                )
            )

        def target_wait_complete(_status):
            return temperature_step_complete

        measurement, samples = await self._measure_palmsens(
            manager,
            method,
            callback,
            temperature_controller,
            temperature_status_callback=handle_status,
            stop_when_temperature_status=(
                None if action.wait_starts_immediately else target_wait_complete
            ),
        )
        if not action.wait_starts_immediately and not temperature_step_complete:
            raise RuntimeError(
                "Temperature did not stabilize before the 24-hour OCV safety limit."
            )

        completion = (
            "Temperature timer completed"
            if action.wait_starts_immediately
            else "Temperature stabilized"
        )
        temperature_c = latest_status.temperature_c if latest_status is not None else None
        setpoint_c = latest_status.setpoint_c if latest_status is not None else None
        suffix = f" at {temperature_c:.2f} C" if temperature_c is not None else ""
        self.progress.emit(
            TemperatureProgress(
                target_c=target_c,
                temperature_c=temperature_c,
                setpoint_c=setpoint_c,
                wait_elapsed_s=wait_s,
                message=f"{completion}{suffix}",
            )
        )
        return measurement, samples

    async def _execute_synchronized_temperature_action(
        self,
        manager,
        method,
        callback,
        temperature_controller,
        action,
        wait_s,
    ):
        target_c = action.target_temperature_c
        self.progress.emit(
            TemperatureProgress(
                target_c=target_c,
                temperature_c=None,
                setpoint_c=None,
                wait_elapsed_s=0.0,
                message=f"Waiting for channels before setting {target_c:.2f} C",
            )
        )

        wait_started_at = None
        latest_status = None
        temperature_step_complete = False

        def handle_status(status):
            nonlocal latest_status, wait_started_at, temperature_step_complete
            latest_status = status
            now = time.monotonic()
            if not action.wait_starts_immediately:
                error_c = abs(status.temperature_c - target_c)
                if error_c <= temperature_controller.settings.tolerance_c:
                    wait_started_at = wait_started_at or now
                else:
                    wait_started_at = None
            wait_elapsed_s = (
                now - wait_started_at if wait_started_at is not None else 0.0
            )
            temperature_step_complete = (
                wait_started_at is not None and wait_elapsed_s >= wait_s
            )
            self.progress.emit(
                TemperatureProgress(
                    target_c=target_c,
                    temperature_c=status.temperature_c,
                    setpoint_c=status.setpoint_c,
                    wait_elapsed_s=wait_elapsed_s,
                    message=temperature_controller.progress_message(
                        status,
                        target_c,
                        wait_elapsed_s,
                        wait_s,
                        action.wait_starts_immediately,
                    ),
                )
            )

        measurement_task = asyncio.create_task(
            self._measure_palmsens(
                manager,
                method,
                callback,
                temperature_controller,
                temperature_status_callback=handle_status,
                stop_when_temperature_status=lambda _status: temperature_step_complete,
            )
        )
        await self.temperature_coordinator.wait_for_all(
            action.execution_index,
            id(self.instrument),
        )

        if self._abort_requested():
            await measurement_task
            raise RuntimeError("Temperature step aborted while waiting for other channels.")

        self.progress.emit(
            TemperatureProgress(
                target_c=target_c,
                temperature_c=None,
                setpoint_c=None,
                wait_elapsed_s=0.0,
                message=f"Setting chamber to {target_c:.2f} C",
            )
        )
        if action.ramp_rate_c_per_min is not None:
            temperature_controller.set_ramp_rate(action.ramp_rate_c_per_min)
        temperature_controller.start()
        temperature_controller.set_target(target_c)
        if action.wait_starts_immediately:
            wait_started_at = time.monotonic()

        measurement, samples = await measurement_task
        if not action.wait_starts_immediately and not temperature_step_complete:
            raise RuntimeError(
                "Temperature did not stabilize before the 24-hour OCV safety limit."
            )

        temperature_c = latest_status.temperature_c if latest_status is not None else None
        setpoint_c = latest_status.setpoint_c if latest_status is not None else None
        suffix = f" at {temperature_c:.2f} C" if temperature_c is not None else ""
        self.progress.emit(
            TemperatureProgress(
                target_c=target_c,
                temperature_c=temperature_c,
                setpoint_c=setpoint_c,
                wait_elapsed_s=wait_s,
                message=f"Temperature step completed{suffix}",
            )
        )
        return measurement, samples

    def abort(self):
        with self._state_lock:
            self.abort_requested = True
            manager = self.manager
            loop = self.loop

        if manager is not None and loop is not None:
            asyncio.run_coroutine_threadsafe(manager.abort(), loop)
            if self.temperature_coordinator is not None:
                loop.call_soon_threadsafe(
                    self.temperature_coordinator.withdraw,
                    id(self.instrument),
                )

    def _abort_requested(self) -> bool:
        with self._state_lock:
            return self.abort_requested
