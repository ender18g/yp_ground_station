# Vehicle companion scripts

The existing ArduCopter and BlueBoat bridge entrypoints keep their vehicle-specific
serial, video, navigation, and configuration behavior. Search missions and
navigation geometry live in the repository's `yp_common/` package.

Use Python 3.10 or newer. From a full repository checkout, install the requirements for the chosen bridge
and run its existing script as usual. To copy just a bridge directory to a Pi,
first build a self-contained bundle from the repository root:

```sh
python3 companion_vehicle_software/bundle_bridge.py arducopter /tmp/arducopter_bcs_bridge
python3 companion_vehicle_software/bundle_bridge.py blueboat /tmp/blueboat_bridge
```

Copy the resulting directory to the Pi. Inside that directory, activate your
virtual environment, run `python -m pip install -r requirements.txt`, then start
`python arducopter_bridge_wServer.py`, `python arducopter_bridge.py`, or
`python blueboat_bridge.py`, as appropriate. The ArduCopter web-server variant
continues to read and save `config.json` in its working directory.

The bundle includes `yp_common/`; copying only the source `*_piScripts` folder
without the shared package is no longer sufficient. Make code changes in the
repository and rebuild the bundle to keep deployments consistent. Bundling
requires a new destination directory so an existing Pi configuration is never
overwritten by accident.
