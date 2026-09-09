# UMAA Bridge Starter

This service is a starter harness for vehicles that speak UMAA over RTI Connext DDS.

What it does:
- Connects to the YP websocket contract at `/ws/vehicle/{vehicle_id}`.
- Forwards UMAA-style telemetry as YP vehicle messages.
- Accepts YP commands and passes them into a pluggable UMAA adapter.

Current adapters:
- `loopback` - local test mode that now moves toward received waypoints, turns gradually, and drains battery over time.
- `rti_connext` - DDS adapter shell that is ready to be wired to the generated UMAA types and command/report services from the RTI starter kit.

How to use it:
1. Set `VEHICLE_ID`, `VEHICLE_TYPE`, and `SERVER_WS_URL`.
2. Leave `UMAA_BACKEND=loopback` to smoke-test the bridge.
3. Switch `UMAA_BACKEND=rti` after you wire the DDS topics for your vehicle profile.

Recommended workflow for UMAA:
1. Run the sim bridge first with `docker compose up sim-umaa`.
2. Use `sim-umaa` in the UI and verify commands, map updates, and SAR routing against loopback telemetry.
3. When the real vehicle arrives, switch to `docker compose --profile umaa-real up umaa-bridge` and fill in the RTI topic names.

Smoke test client:
- `python services/umaa_bridge/sim_umaa_smoke_test.py`
- It connects to `sim-umaa`, sends a waypoint 25 m east, prints telemetry for a few seconds, then sends RTB.
- Override `--waypoint-distance-m`, `--waypoint-bearing-deg`, or `--rtb-wait-s` if you want a longer or shorter run.

Loopback tuning knobs:
- `LOOPBACK_SPEED_MPS`
- `LOOPBACK_TURN_RATE_DPS`
- `LOOPBACK_ARRIVAL_RADIUS_M`
- `LOOPBACK_BATTERY_DRAIN_PER_M`
- `LOOPBACK_BATTERY_DRAIN_PER_S`

RTI wiring knobs:
- `RTI_DOMAIN_ID`
- `RTI_QOS_FILE`
- `RTI_COMMAND_TOPIC`
- `RTI_ACK_TOPIC`
- `RTI_STATUS_TOPIC`
- `RTI_NAVSATFIX_TOPIC`
- `RTI_BATTERY_TOPIC`
- `RTI_HEARTBEAT_TOPIC`
- `RTI_SOURCE_GUID`
- `RTI_PUBLISHER_NAME`
- `RTI_SUBSCRIBER_NAME`

The DDS side is intentionally isolated in `RtiConnextUmaaAdapter` so the topic map for a specific UMAA vehicle can be added without touching the websocket contract or the UI.