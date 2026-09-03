import asyncio
import gzip
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from app import main


class FakeRecord:
    def __init__(self, values):
        self.values = values

    def get_time(self):
        return self.values["_time"]


class FakeQueryApi:
    def __init__(self, records=None, error=None):
        self.records = records or []
        self.error = error

    def query_stream(self, **kwargs):
        if self.error:
            raise self.error
        return iter(self.records)


class FlightLogExportTests(unittest.TestCase):
    def test_range_requires_start_and_end(self):
        with self.assertRaisesRegex(ValueError, "start and end"):
            main._log_range(None, None, None)

    def test_range_rejects_reversed_timestamps(self):
        with self.assertRaisesRegex(ValueError, "start must be before end"):
            main._log_range("2026-09-02T10:00:00Z", "2026-09-02T09:00:00Z", None)

    def test_export_requires_permission(self):
        response = asyncio.run(main.export_log(last_hours=1, authorization=None))
        self.assertEqual(response.status_code, 401)

    def test_empty_export_returns_not_found(self):
        with patch.object(main, "query_api", FakeQueryApi()):
            response = asyncio.run(main.export_log(last_hours=1, authorization="Bearer token"))
        self.assertEqual(response.status_code, 404)

    def test_query_failure_returns_service_unavailable(self):
        with patch.object(main, "require_permission", return_value=None), patch.object(
            main, "query_api", FakeQueryApi(error=RuntimeError("offline"))
        ):
            response = asyncio.run(main.export_log(last_hours=1, authorization="Bearer token"))
        self.assertEqual(response.status_code, 503)

    def test_successful_export_has_metadata_and_message_records(self):
        record = FakeRecord(
            {
                "_time": datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc),
                "vehicle_id": "boat-01",
                "vehicle_type": "usv",
                "topic": "/boat-01/nav",
                "msg_type": "sensor/Nav",
                "latitude": 38.9,
                "longitude": -76.4,
            }
        )
        with patch.object(main, "require_permission", return_value=None), patch.object(
            main, "query_api", FakeQueryApi([record])
        ):
            response = asyncio.run(main.export_log(last_hours=1, authorization="Bearer token"))
            body = asyncio.run(collect_response(response))

        lines = [json.loads(line) for line in gzip.decompress(body).decode().splitlines()]
        self.assertEqual(lines[0]["format"], "yp-ground-station-log")
        self.assertEqual(lines[1]["fields"]["latitude"], 38.9)
        self.assertNotIn("topic", lines[1])
        self.assertNotIn("message_type", lines[1])
        self.assertIn("attachment", response.headers["content-disposition"])

    def test_heartbeat_records_are_excluded_from_export(self):
        records = [
            FakeRecord({"_time": datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc), "vehicle_id": "boat-01", "msg_type": "Heartbeat", "topic": "/heartbeat"}),
            FakeRecord({"_time": datetime(2026, 9, 2, 12, 0, 1, tzinfo=timezone.utc), "vehicle_id": "boat-01", "msg_type": "sensor/Nav", "topic": "/nav", "latitude": 38.9}),
        ]
        with patch.object(main, "require_permission", return_value=None), patch.object(
            main, "query_api", FakeQueryApi(records)
        ):
            response = asyncio.run(main.export_log(last_hours=1, authorization="Bearer token"))
            body = asyncio.run(collect_response(response))

        lines = [json.loads(line) for line in gzip.decompress(body).decode().splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[1]["fields"]["latitude"], 38.9)

    def test_writer_ignores_heartbeat_payloads(self):
        while not main._influx_write_queue.empty():
            main._influx_write_queue.get_nowait()
        with patch.object(main, "write_api", object()):
            main.write_influx(
                {
                    "vehicle_id": "boat-01",
                    "vehicle_type": "usv",
                    "topic": "/boat-01/heartbeat",
                    "type": "yp_ground_station/msg/Heartbeat",
                    "stamp": 1756814400.0,
                    "msg": {"mode": "loiter"},
                }
            )
        self.assertTrue(main._influx_write_queue.empty())


async def collect_response(response):
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode())
    return b"".join(chunks)


if __name__ == "__main__":
    unittest.main()
