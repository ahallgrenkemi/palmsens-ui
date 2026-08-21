# PalmSens UI

Small PySide6 desktop app for running PalmSens measurements, plotting live data, and exporting results to Battery Data Format-style CSV files.

This project mainly builds upon three related works:

- [PyPalmSens](https://github.com/PalmSens/PalmSens_SDK/releases)
- [Aurora Method Builder](https://github.com/Interphases-Lab/protocol-creator)
- [Battery Data Format](https://github.com/battery-data-alliance/battery-data-format)
- [Aurora Unicycler](https://github.com/EmpaEConversion/aurora-unicycler)

The method builder is maintained in its own repository and installed automatically from the pinned Git revision in `pyproject.toml`.

Some of the functionality includes:

- Connecting to PalmSens instruments through PyPalmSens.
- Runs built-in PalmSens methods, pasted MethodSCRIPT, or imported method packages.
- Converts Aurora packages into step-by-step execution so each PalmSens-compatible step runs as its own MethodSCRIPT measurement.
- Groups those step measurements into one logical run for plotting and export.
- Handles time-series data and EIS data separately, while preserving shared step metadata such as step id, step type, and execution index.
- Exports measurement results based on Battery Data Format.

## Temperature Chamber

Temperature steps are coordinated directly by the app through `temperature_chamber/temperature_controller.py`. Enable the Arduino temperature chamber in the Aurora package run dialog, then run the method normally. While the chamber ramps or waits, the PalmSens channel runs open-circuit potentiometry so voltage and chamber temperature continue to be recorded and plotted as a normal step.

Leave the serial port blank to auto-detect Arduino USB serial devices, or enter a port such as `COM31`.

While the chamber is enabled, it is polled during every PalmSens measurement step, including steps that do not change the temperature. Each exported row uses the latest chamber temperature and setpoint available at that time, and both values are included in BDF exports. The setpoint is stored in T1 and the current temperature is stored in the ambient temperature fields.

## Run

Python 3.12 or newer is required. From the repository root, the shortest setup is with [uv](https://docs.astral.sh/uv/):

```sh
uv run palmsens-ui
```

`uv` creates the environment and installs the dependencies automatically on the first run.

Alternatively, install it with `pip`:

```sh
python -m venv .venv
```

Activate the environment:

```sh
# Linux
source .venv/bin/activate

# Windows PowerShell
.venv\Scripts\Activate.ps1
```

Then install and run the application:

```sh
python -m pip install -e .
palmsens-ui
```

The standalone method builder is installed into the same environment. Run it with `uv`:

```sh
uv run aurora-method-builder
```

Or, from the activated `pip` environment:

```sh
aurora-method-builder
```

# TODOs
- The current implementation of the temperature chamber is hardwired based on the firmware run on local hardware. In a future implementation communication with external hardware could be abstracted through an API, which would allow for integration of external hardware and custom steps. This would mainly require refactoring the storage of measured data, to allow for "custom" data.
- Since the system makes use both Palmsens-native data and non-Palmsens native data (temperature) the handling of data can be messy. A solution would be further abstraction to make the system "blind" to where the data came from with the use of helpers.
- Live data currently only uses a live dataset for the current measurement. This makes the system unable to display past measurements during running. A solution would be to instead use unified datasets, even during live plotting.
- Importing data relies on psession but they cant save externally recorded data like temp. So building a local exporter or importer might be a solution, or modify the psession file.
