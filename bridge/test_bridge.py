"""Self-contained tests for the decryption and id derivation in bridge.py.

Run from this directory:

    python3 -m unittest -v
"""

import base64
import hashlib
import os
import sys
import unittest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bridge  # noqa: E402

CURVE = ec.SECP224R1()


def make_report(device_private_key, timestamp, confidence,
                latitude, longitude, accuracy, status, version_byte=None):
    """Build an OpenHaystack-style encrypted report payload (base64)."""
    ephemeral_private = ec.generate_private_key(CURVE)
    ephemeral_public = ephemeral_private.public_key()
    ephemeral_bytes = ephemeral_public.public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint)

    shared = ephemeral_private.exchange(
        ec.ECDH(), device_private_key.public_key())
    symmetric = hashlib.sha256(
        shared + b"\x00\x00\x00\x01" + ephemeral_bytes).digest()

    plaintext = (
        int(latitude * 1e7).to_bytes(4, "big", signed=True)
        + int(longitude * 1e7).to_bytes(4, "big", signed=True)
        + bytes([accuracy, status]))

    encrypted = AESGCM(symmetric[:16]).encrypt(
        symmetric[16:], plaintext, None)

    body = (
        (timestamp - bridge.APPLE_EPOCH_OFFSET).to_bytes(4, "big", signed=True)
        + bytes([confidence])
        + ephemeral_bytes
        + encrypted)

    if version_byte is not None:
        body = body[:4] + bytes([version_byte]) + body[4:]

    return base64.b64encode(body).decode("ascii")


class BridgeTest(unittest.TestCase):

    def setUp(self):
        self.device_private = ec.generate_private_key(CURVE)
        self.private_b64 = base64.b64encode(
            self.device_private.private_numbers().private_value.to_bytes(
                28, "big")).decode("ascii")

    def test_derive_apple_id(self):
        apple_id = bridge.derive_apple_id(self.private_b64)
        adv_x = self.device_private.public_key().public_numbers().x
        expected = base64.b64encode(
            hashlib.sha256(adv_x.to_bytes(28, "big")).digest()).decode("ascii")
        self.assertEqual(apple_id, expected)

    def _round_trip(self, version_byte):
        timestamp = 1750000000
        payload = make_report(
            self.device_private, timestamp, 7,
            48.8566, 2.3522, 12, 0x40, version_byte=version_byte)
        decoded = bridge.decrypt_report(self.device_private, payload)
        self.assertEqual(decoded["timestamp"], timestamp)
        self.assertEqual(decoded["confidence"], 7)
        self.assertAlmostEqual(decoded["latitude"], 48.8566, places=6)
        self.assertAlmostEqual(decoded["longitude"], 2.3522, places=6)
        self.assertEqual(decoded["accuracy"], 12)
        self.assertEqual(decoded["status"], 0x40)
        self.assertEqual(decoded["battery"], "medium")

    def test_round_trip_short_payload(self):
        self._round_trip(version_byte=None)

    def test_round_trip_long_payload(self):
        self._round_trip(version_byte=2)

    def test_negative_coordinates(self):
        timestamp = 1750000000
        payload = make_report(
            self.device_private, timestamp, 1, -33.8688, -70.6693, 5, 0x80)
        decoded = bridge.decrypt_report(self.device_private, payload)
        self.assertAlmostEqual(decoded["latitude"], -33.8688, places=6)
        self.assertAlmostEqual(decoded["longitude"], -70.6693, places=6)
        self.assertEqual(decoded["battery"], "low")

    def test_battery_from_status(self):
        self.assertIsNone(bridge.battery_from_status(0))
        self.assertEqual(bridge.battery_from_status(0x00 | 0x20), "ok")
        self.assertEqual(bridge.battery_from_status(0x40 | 0x20), "medium")
        self.assertEqual(bridge.battery_from_status(0x80 | 0x20), "low")
        self.assertEqual(bridge.battery_from_status(0xC0 | 0x20), "critical")

    def test_posted_timestamp_matches_report(self):
        timestamp = 1750000000
        payload = make_report(
            self.device_private, timestamp, 1, 48.8566, 2.3522, 3, 0x20)
        decoded = bridge.decrypt_report(self.device_private, payload)
        params = bridge.build_params("airtag", decoded)
        self.assertEqual(params["timestamp"], timestamp * 1000)
        self.assertEqual(params["id"], "airtag")
        self.assertEqual(params["lat"], "48.8566000")
        self.assertEqual(params["lon"], "2.3522000")
        self.assertEqual(params["batt"], 100)


class DeviceParsingTest(unittest.TestCase):

    def _key(self):
        return base64.b64encode(
            ec.generate_private_key(CURVE).private_numbers().private_value
            .to_bytes(28, "big")).decode("ascii")

    def test_flat_private_key_map(self):
        key = self._key()
        devices = bridge.parse_devices({"airtag1": key}, "devices.json")
        self.assertEqual(devices, {"airtag1": [key]})

    def test_device_with_additional_keys(self):
        main = self._key()
        extra = [self._key(), self._key()]
        devices = bridge.parse_devices(
            {"airtag1": [main, *extra]}, "devices.json")
        self.assertEqual(devices, {"airtag1": [main, *extra]})

    def test_device_with_single_key_list(self):
        main = self._key()
        devices = bridge.parse_devices({"airtag1": [main]}, "devices.json")
        self.assertEqual(devices, {"airtag1": [main]})

    def test_mixed_flat_and_list(self):
        solo = self._key()
        main = self._key()
        extra = [self._key()]
        devices = bridge.parse_devices(
            {"solo": solo, "Z7NYAJ": [main, *extra]}, "devices.json")
        self.assertEqual(devices, {"solo": [solo], "Z7NYAJ": [main, *extra]})

    def test_all_keys_derive_distinct_ids(self):
        main = self._key()
        other = self._key()
        devices = bridge.parse_devices(
            {"tag": [main, other]}, "devices.json")
        ids = {bridge.derive_apple_id(key) for key in devices["tag"]}
        self.assertEqual(len(ids), 2)

    def test_invalid_device_value(self):
        with self.assertRaises(ValueError):
            bridge.parse_devices({"tag": 123}, "devices.json")

    def test_empty_list(self):
        with self.assertRaises(ValueError):
            bridge.parse_devices({"tag": []}, "devices.json")

    def test_list_with_non_string(self):
        with self.assertRaises(ValueError):
            bridge.parse_devices(
                {"tag": [self._key(), 123]}, "devices.json")

    def test_empty_string(self):
        with self.assertRaises(ValueError):
            bridge.parse_devices({"tag": ""}, "devices.json")

    def test_top_level_must_be_object(self):
        with self.assertRaises(ValueError):
            bridge.parse_devices([{"name": "tag", "privateKey": self._key()}],
                                 "devices.json")


if __name__ == "__main__":
    unittest.main()
