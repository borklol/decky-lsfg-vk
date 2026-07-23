import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path, PurePosixPath
from subprocess import CompletedProcess
from unittest.mock import patch


sys.modules.setdefault(
    "decky",
    types.SimpleNamespace(logger=logging.getLogger("decky-test")),
)

from py_modules.lsfg_vk.config_schema import ConfigurationManager, DEFAULT_PROFILE_NAME
from py_modules.lsfg_vk.configuration import ConfigurationService
from py_modules.lsfg_vk.installation import InstallationService
from py_modules.lsfg_vk.flatpak_service import FlatpakService


class V2ConfigTests(unittest.TestCase):
    def test_generates_v2_profiles_and_preserves_off_state(self):
        config = ConfigurationManager.get_defaults()
        config["dll"] = "/games/Lossless Scaling/Lossless.dll"
        config["multiplier"] = 1

        content = ConfigurationManager.generate_toml_content(config)

        self.assertIn("version = 2", content)
        self.assertIn("[[profile]]", content)
        self.assertIn(f'name = "{DEFAULT_PROFILE_NAME}"', content)
        self.assertIn("allow_fp16 = true", content)
        self.assertIn("enabled = false", content)
        self.assertIn("multiplier = 2", content)
        self.assertIn('pacing = "none"', content)
        self.assertNotIn("[[game]]", content)
        self.assertNotIn("hdr_mode", content)
        self.assertNotIn("experimental_present_mode", content)

        parsed = ConfigurationManager.parse_toml_content(content)
        self.assertEqual(1, parsed["multiplier"])

    def test_migrates_legacy_v1_config(self):
        legacy = """
version = 1

[global]
current_profile = "Emulator"
dll = "/games/Lossless Scaling/Lossless.dll"
no_fp16 = false

[[game]]
exe = "Emulator"
multiplier = 3
flow_scale = 0.75
performance_mode = true
"""

        profile_data = ConfigurationManager.parse_toml_content_multi_profile(legacy)
        migrated = ConfigurationManager.generate_toml_content_multi_profile(profile_data)

        self.assertEqual("Emulator", profile_data["current_profile"])
        self.assertEqual(3, profile_data["profiles"]["Emulator"]["multiplier"])
        self.assertIn('name = "Emulator"', migrated)
        self.assertIn("multiplier = 3", migrated)
        self.assertIn("flow_scale = 0.75", migrated)
        self.assertIn("performance_mode = true", migrated)

    def test_launch_script_selects_v2_profile(self):
        service = ConfigurationService(logger=logging.getLogger("decky-test"))
        service.config_file_path = PurePosixPath("/home/deck/.config/lsfg-vk/conf.toml")
        config = ConfigurationManager.get_defaults()
        config["multiplier"] = 2
        profile_data = {
            "current_profile": "3DS Emulator",
            "profiles": {"3DS Emulator": config},
            "global_config": {},
        }

        script = service._generate_script_content_for_profile(profile_data)

        self.assertIn(
            "export LSFGVK_CONFIG=/home/deck/.config/lsfg-vk/conf.toml",
            script,
        )
        self.assertIn("export LSFGVK_PROFILE='3DS Emulator'", script)
        self.assertNotIn("LSFG_PROCESS", script)
        self.assertNotIn("DISABLE_LSFGVK", script)

        profile_data["profiles"]["3DS Emulator"]["multiplier"] = 1
        script = service._generate_script_content_for_profile(profile_data)
        self.assertIn("export DISABLE_LSFGVK=1", script)

    def test_installation_preserves_selected_profile_in_launch_script(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / ".config" / "lsfg-vk" / "conf.toml"
            script_path = root / "lsfg"
            config_path.parent.mkdir(parents=True)

            config = ConfigurationManager.get_defaults()
            config["multiplier"] = 2
            profile_data = {
                "current_profile": "3DS Emulator",
                "profiles": {"3DS Emulator": config},
                "global_config": {},
            }
            config_path.write_text(
                ConfigurationManager.generate_toml_content_multi_profile(profile_data),
                encoding="utf-8",
            )

            service = InstallationService(logger=logging.getLogger("decky-test"))
            service.user_home = root
            service.config_file_path = config_path
            service.lsfg_launch_script_path = script_path
            service._create_lsfg_launch_script()

            script = script_path.read_text(encoding="utf-8")
            self.assertIn("export LSFGVK_PROFILE='3DS Emulator'", script)
            self.assertNotIn("DISABLE_LSFGVK", script)

    def test_flatpak_override_pins_the_selected_v2_profile(self):
        service = FlatpakService(logger=logging.getLogger("decky-test"))
        service.check_flatpak_available = lambda: True
        commands = []

        def run_command(args, **kwargs):
            commands.append(args)
            return CompletedProcess(args, 0, stdout="", stderr="")

        service._run_flatpak_command = run_command

        with patch("os.path.expanduser", return_value="/home/deck"):
            result = service.set_app_override(
                "org.azahar_emu.Azahar",
                "3DS Emulator",
            )

        self.assertTrue(result["success"])
        self.assertIn(
            [
                "override",
                "--user",
                "--env=LSFGVK_CONFIG=/home/deck/.config/lsfg-vk/conf.toml",
                "--env=LSFGVK_PROFILE=3DS Emulator",
                "org.azahar_emu.Azahar",
            ],
            commands,
        )

    def test_flatpak_override_status_requires_a_v2_profile(self):
        service = FlatpakService(logger=logging.getLogger("decky-test"))
        service.flatpak_command = "flatpak"
        output = """
[Context]
filesystems=/home/deck/.local/share/Steam/steamapps/common/Lossless Scaling/Lossless.dll;/home/deck/.config/lsfg-vk;/home/deck/lsfg;

[Environment]
LSFGVK_CONFIG=/home/deck/.config/lsfg-vk/conf.toml
LSFGVK_PROFILE=3DS Emulator
"""
        service._run_flatpak_command = lambda args, **kwargs: CompletedProcess(
            args,
            0,
            stdout=output,
            stderr="",
        )

        with patch("os.path.expanduser", return_value="/home/deck"):
            status = service._check_app_override_status("org.azahar_emu.Azahar")

        self.assertTrue(status["filesystem"])
        self.assertTrue(status["config_env"])
        self.assertTrue(status["env"])


if __name__ == "__main__":
    unittest.main()
