import os
import sys
import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.check_usb_demo_feasibility import (
    collect_appliance_snapshot,
    compare_macos_snapshots,
    evaluate_appliance_snapshot,
)


class UsbDemoWorkflowTests(unittest.TestCase):
    def test_evaluate_appliance_snapshot_rejects_host_only_connector(self) -> None:
        report = evaluate_appliance_snapshot(
            {
                "model": "Raspberry Pi 4 Model B Rev 1.5",
                "udc_names": ["fe980000.usb"],
                "output_devices": [],
            },
            connector_port="usb-a-host",
        )
        self.assertFalse(report["passed"])
        self.assertIn("connector_port_host_only", report["reasons"])

    def test_evaluate_appliance_snapshot_rejects_missing_udc(self) -> None:
        report = evaluate_appliance_snapshot(
            {
                "model": "Raspberry Pi 4 Model B Rev 1.5",
                "udc_names": [],
                "output_devices": [],
            },
            connector_port="usb-c-otg",
        )
        self.assertFalse(report["passed"])
        self.assertIn("no_udc_controller", report["reasons"])

    def test_evaluate_appliance_snapshot_requires_expected_output_device_when_requested(self) -> None:
        report = evaluate_appliance_snapshot(
            {
                "model": "Raspberry Pi 4 Model B Rev 1.5",
                "udc_names": ["fe980000.usb"],
                "output_devices": [
                    {"name": "UAC2Gadget", "hostapi": "ALSA", "max_output_channels": 2},
                ],
            },
            connector_port="usb-c-otg",
            required_output_device="FocusField USB Mic",
        )
        self.assertFalse(report["passed"])
        self.assertIn("required_output_device_missing", report["reasons"])

    def test_collect_appliance_snapshot_captures_output_enumeration_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            model_path = root / "model"
            model_path.write_text("Raspberry Pi 4 Model B Rev 1.5\x00", encoding="utf-8")
            udc_root = root / "udc"
            udc_root.mkdir()
            (udc_root / "fe980000.usb").mkdir()

            with mock.patch(
                "scripts.check_usb_demo_feasibility.list_output_devices",
                side_effect=RuntimeError("portaudio unavailable"),
            ):
                snapshot = collect_appliance_snapshot(
                    model_path=model_path,
                    udc_root=udc_root,
                    runner=lambda _cmd: {
                        "available": True,
                        "returncode": 0,
                        "stdout": "",
                        "stderr": "",
                        "command": ["lsusb", "-t"],
                    },
                )

        self.assertEqual(snapshot["model"], "Raspberry Pi 4 Model B Rev 1.5")
        self.assertEqual(snapshot["udc_names"], ["fe980000.usb"])
        self.assertEqual(snapshot["output_devices"], [])
        self.assertIn("portaudio unavailable", snapshot["output_devices_error"])

    def test_evaluate_appliance_snapshot_rejects_output_inventory_failures(self) -> None:
        report = evaluate_appliance_snapshot(
            {
                "model": "Raspberry Pi 4 Model B Rev 1.5",
                "udc_names": ["fe980000.usb"],
                "output_devices": [],
                "output_devices_error": "RuntimeError: portaudio unavailable",
            },
            connector_port="usb-c-otg",
        )
        self.assertFalse(report["passed"])
        self.assertIn("output_device_inventory_failed", report["reasons"])

    def test_compare_macos_snapshots_detects_new_focusfield_audio_device(self) -> None:
        before = {
            "usb": {"items": ["MacBook Pro USB-C"]},
            "audio": {"devices": ["MacBook Pro Microphone"]},
        }
        after = {
            "usb": {"items": ["MacBook Pro USB-C", "FocusField USB Mic"]},
            "audio": {"devices": ["MacBook Pro Microphone", "FocusField USB Mic"]},
        }
        report = compare_macos_snapshots(before, after, expected_device_name="FocusField USB Mic")
        self.assertTrue(report["passed"])
        self.assertTrue(report["appeared_in_audio"])
        self.assertTrue(report["appeared_in_usb"])

    def test_compare_macos_snapshots_rejects_device_that_never_appears(self) -> None:
        before = {
            "usb": {"items": ["MacBook Pro USB-C"]},
            "audio": {"devices": ["MacBook Pro Microphone"]},
        }
        after = {
            "usb": {"items": ["MacBook Pro USB-C"]},
            "audio": {"devices": ["MacBook Pro Microphone"]},
        }
        report = compare_macos_snapshots(before, after, expected_device_name="FocusField USB Mic")
        self.assertFalse(report["passed"])
        self.assertIn("expected_device_missing_after_attach", report["reasons"])

    def test_install_service_creates_gadget_unit_for_usb_mic_even_without_exact_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            systemd_dir = root / "systemd"
            systemd_dir.mkdir()
            systemctl_log = root / "systemctl.log"
            systemctl_bin = root / "fake-systemctl"
            systemctl_bin.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" >> \"$FOCUSFIELD_TEST_SYSTEMCTL_LOG\"\n",
                encoding="utf-8",
            )
            systemctl_bin.chmod(0o755)

            config_path = root / "demo.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        "runtime:",
                        "  mode: mac_loopback_dev",
                        "audio:",
                        "  models:",
                        "    allow_runtime_downloads: true",
                        "vision:",
                        "  models:",
                        "    allow_runtime_downloads: true",
                        "output:",
                        "  sink: usb_mic",
                        "  usb_mic:",
                        "    device_selector:",
                        "      match_substring: USB",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            script = Path("scripts/install_systemd_service.sh").resolve()
            env = {
                **os.environ,
                "FOCUSFIELD_PYTHON_BIN": sys.executable,
                "FOCUSFIELD_SYSTEMD_DIR": str(systemd_dir),
                "FOCUSFIELD_SYSTEMCTL_BIN": str(systemctl_bin),
                "FOCUSFIELD_SUDO_BIN": "",
                "FOCUSFIELD_TEST_SYSTEMCTL_LOG": str(systemctl_log),
            }
            completed = subprocess.run(
                ["bash", str(script), "focusfield", str(config_path)],
                cwd=str(Path(__file__).resolve().parents[1]),
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

            service_unit = (systemd_dir / "focusfield.service").read_text(encoding="utf-8")
            gadget_unit = (systemd_dir / "focusfield-usb-gadget.service").read_text(encoding="utf-8")
            self.assertIn("Requires=focusfield-usb-gadget.service", service_unit)
            self.assertIn('Environment="FOCUSFIELD_ENABLE_USB_GADGET=1"', service_unit)
            self.assertIn('Environment="FOCUSFIELD_USB_GADGET_PRODUCT_NAME=FocusField USB Mic"', service_unit)
            self.assertIn('Environment="FOCUSFIELD_USB_GADGET_PRODUCT_NAME=FocusField USB Mic"', gadget_unit)

    def test_install_service_refuses_host_only_usb_demo_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            systemd_dir = root / "systemd"
            systemd_dir.mkdir()
            systemctl_log = root / "systemctl.log"
            systemctl_bin = root / "fake-systemctl"
            systemctl_bin.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" >> \"$FOCUSFIELD_TEST_SYSTEMCTL_LOG\"\n",
                encoding="utf-8",
            )
            systemctl_bin.chmod(0o755)

            config_path = root / "demo.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        "runtime:",
                        "  mode: mac_loopback_dev",
                        "audio:",
                        "  models:",
                        "    allow_runtime_downloads: true",
                        "vision:",
                        "  models:",
                        "    allow_runtime_downloads: true",
                        "output:",
                        "  sink: usb_mic",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            script = Path("scripts/install_systemd_service.sh").resolve()
            env = {
                **os.environ,
                "FOCUSFIELD_PYTHON_BIN": sys.executable,
                "FOCUSFIELD_SYSTEMD_DIR": str(systemd_dir),
                "FOCUSFIELD_SYSTEMCTL_BIN": str(systemctl_bin),
                "FOCUSFIELD_SUDO_BIN": "",
                "FOCUSFIELD_TEST_SYSTEMCTL_LOG": str(systemctl_log),
                "FOCUSFIELD_USB_GADGET_CONNECTOR_PORT": "usb-a-host",
            }
            completed = subprocess.run(
                ["bash", str(script), "focusfield", str(config_path)],
                cwd=str(Path(__file__).resolve().parents[1]),
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 5)
            self.assertIn("host-only", completed.stderr)
            self.assertFalse((systemd_dir / "focusfield.service").exists())
            self.assertFalse((systemd_dir / "focusfield-usb-gadget.service").exists())

    def test_install_service_removes_stale_gadget_unit_when_sink_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            systemd_dir = root / "systemd"
            systemd_dir.mkdir()
            systemctl_log = root / "systemctl.log"
            systemctl_bin = root / "fake-systemctl"
            systemctl_bin.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" >> \"$FOCUSFIELD_TEST_SYSTEMCTL_LOG\"\n",
                encoding="utf-8",
            )
            systemctl_bin.chmod(0o755)

            usb_config = root / "usb.yaml"
            usb_config.write_text(
                "\n".join(
                    [
                        "runtime:",
                        "  mode: mac_loopback_dev",
                        "audio:",
                        "  models:",
                        "    allow_runtime_downloads: true",
                        "vision:",
                        "  models:",
                        "    allow_runtime_downloads: true",
                        "output:",
                        "  sink: usb_mic",
                        "  usb_mic:",
                        "    device_selector:",
                        "      match_substring: USB",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            host_config = root / "host.yaml"
            host_config.write_text(
                "\n".join(
                    [
                        "runtime:",
                        "  mode: mac_loopback_dev",
                        "audio:",
                        "  models:",
                        "    allow_runtime_downloads: true",
                        "vision:",
                        "  models:",
                        "    allow_runtime_downloads: true",
                        "output:",
                        "  sink: host_loopback",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            script = Path("scripts/install_systemd_service.sh").resolve()
            env = {
                **os.environ,
                "FOCUSFIELD_PYTHON_BIN": sys.executable,
                "FOCUSFIELD_SYSTEMD_DIR": str(systemd_dir),
                "FOCUSFIELD_SYSTEMCTL_BIN": str(systemctl_bin),
                "FOCUSFIELD_SUDO_BIN": "",
                "FOCUSFIELD_TEST_SYSTEMCTL_LOG": str(systemctl_log),
            }
            first = subprocess.run(
                ["bash", str(script), "focusfield", str(usb_config)],
                cwd=str(Path(__file__).resolve().parents[1]),
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertTrue((systemd_dir / "focusfield-usb-gadget.service").exists())

            second = subprocess.run(
                ["bash", str(script), "focusfield", str(host_config)],
                cwd=str(Path(__file__).resolve().parents[1]),
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertFalse((systemd_dir / "focusfield-usb-gadget.service").exists())
            self.assertIn("disable --now focusfield-usb-gadget", systemctl_log.read_text(encoding="utf-8"))

    def test_focusfield_boot_refuses_host_only_usb_demo_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "demo.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        "runtime:",
                        "  mode: mac_loopback_dev",
                        "audio:",
                        "  models:",
                        "    allow_runtime_downloads: true",
                        "vision:",
                        "  models:",
                        "    allow_runtime_downloads: true",
                        "output:",
                        "  sink: usb_mic",
                        "  usb_mic:",
                        "    device_selector:",
                        "      match_substring: USB",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            script = Path("scripts/focusfield_boot.sh").resolve()
            completed = subprocess.run(
                ["bash", str(script), str(config_path)],
                cwd=str(Path(__file__).resolve().parents[1]),
                env={
                    **os.environ,
                    "FOCUSFIELD_PYTHON_BIN": sys.executable,
                    "FOCUSFIELD_USB_GADGET_CONNECTOR_PORT": "usb-a-host",
                },
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 5)
            self.assertIn("USB demo feasibility gate failed", completed.stderr)


if __name__ == "__main__":
    unittest.main()
