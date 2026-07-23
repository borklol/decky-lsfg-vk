"""
Installation service for lsfg-vk.
"""

import platform
import shutil
import traceback
import tarfile
from pathlib import Path
from typing import Dict, Any

from .base_service import BaseService
from .constants import (
    ARCHIVE_FILENAME, BIN_DIR, CLI_FILENAME, JSON_FILENAME, LIB_FILENAME,
    LEGACY_JSON_FILENAME, LEGACY_LIB_FILENAME
)
from .config_schema import ConfigurationManager
from .types import InstallationResponse, UninstallationResponse, InstallationCheckResponse


class InstallationService(BaseService):
    """Service for handling lsfg-vk installation and uninstallation"""
    
    def __init__(self, logger=None):
        super().__init__(logger)
        
        self.lib_file = self.local_lib_dir / LIB_FILENAME
        self.json_file = self.local_share_dir / JSON_FILENAME
        self.cli_file = self.local_bin_dir / CLI_FILENAME
        self.legacy_lib_file = self.local_lib_dir / LEGACY_LIB_FILENAME
        self.legacy_json_file = self.local_share_dir / LEGACY_JSON_FILENAME
    
    def install(self) -> InstallationResponse:
        """Install lsfg-vk v2 from the pinned upstream archive.
        
        Returns:
            InstallationResponse with success status and message/error
        """
        try:
            if not self._is_x86_64_architecture():
                return self._error_response(
                    InstallationResponse,
                    "The LSFG-VK v2 test build currently supports x86_64 only.",
                    message=""
                )

            plugin_dir = Path(__file__).parent.parent.parent
            archive_path = plugin_dir / BIN_DIR / ARCHIVE_FILENAME
            
            if not archive_path.exists():
                error_msg = f"{ARCHIVE_FILENAME} not found at {archive_path}"
                self.log.error(error_msg)
                return self._error_response(InstallationResponse, error_msg, message="")
            
            self._ensure_directories()
            
            self._extract_and_install_files(archive_path)
            
            self._create_config_file()
            
            self._create_lsfg_launch_script()

            # Remove the v1 manifest only after v2 and its configuration are ready.
            # Leaving both implicit layers installed would load frame generation twice.
            self._remove_if_exists(self.legacy_json_file)
            self._remove_if_exists(self.legacy_lib_file)
            
            self.log.info("lsfg-vk installed successfully")
            return self._success_response(InstallationResponse, "lsfg-vk installed successfully")
            
        except (OSError, tarfile.TarError, shutil.Error) as e:
            error_msg = f"Error installing lsfg-vk: {str(e)}"
            self.log.error(error_msg)
            return self._error_response(InstallationResponse, str(e), message="")
        except Exception as e:
            error_msg = f"Unexpected error installing lsfg-vk: {str(e)}"
            self.log.error(error_msg)
            return self._error_response(InstallationResponse, str(e), message="")
    
    def _is_x86_64_architecture(self) -> bool:
        """Return whether this binary bundle supports the current machine."""
        return platform.machine().lower() in {"x86_64", "amd64"}
    
    def _extract_and_install_files(self, archive_path: Path) -> None:
        """Install only the v2 layer, manifest, and CLI from the release archive.
        
        Args:
            archive_path: Path to the pinned LSFG-VK v2 tar.xz archive
            
        Raises:
            tarfile.TarError: If the archive is corrupted
            OSError: If file operations fail
        """
        archive_files = {
            "lib/liblsfg-vk-layer.so": (self.lib_file, 0o755),
            "share/vulkan/implicit_layer.d/VkLayer_LSFGVK_frame_generation.json": (
                self.json_file,
                0o644
            ),
            "bin/lsfg-vk-cli": (self.cli_file, 0o755),
        }
        installed = set()
        
        with tarfile.open(archive_path, "r:xz") as archive:
            for member in archive.getmembers():
                member_name = member.name.removeprefix("./")
                target = archive_files.get(member_name)
                if not target:
                    continue

                destination, mode = target
                source = archive.extractfile(member)
                if source is None:
                    raise tarfile.ReadError(f"Unable to read {member.name}")

                with source, open(destination, "wb") as output:
                    shutil.copyfileobj(source, output)
                destination.chmod(mode)
                installed.add(member_name)
                self.log.info(f"Installed {member_name} to {destination}")

        missing = set(archive_files) - installed
        if missing:
            raise tarfile.ReadError(
                f"LSFG-VK archive is missing required files: {', '.join(sorted(missing))}"
            )
    
    def _create_config_file(self) -> None:
        """Create or update the TOML config file in ~/.config/lsfg-vk with default configuration and detected DLL path
        
        If a config file already exists, preserve existing profiles and only update global settings like DLL path.
        """
        # Import here to avoid circular imports
        from .dll_detection import DllDetectionService
        
        # Try to detect DLL path
        dll_service = DllDetectionService(self.log)
        
        # Check if config file already exists
        if self.config_file_path.exists():
            try:
                backup_path = self.config_file_path.with_suffix(".v1.toml.bak")
                if not backup_path.exists():
                    shutil.copy2(self.config_file_path, backup_path)
                    self.log.info(f"Backed up existing configuration to {backup_path}")

                # Read existing config to preserve user profiles
                content = self.config_file_path.read_text(encoding='utf-8')
                existing_profile_data = ConfigurationManager.parse_toml_content_multi_profile(content)
                self.log.info(f"Found existing config file, preserving user profiles")
                
                # Create merged profile data that preserves user settings but adds any new fields
                merged_profile_data = self._merge_config_with_defaults(existing_profile_data, dll_service)
                
                # Generate TOML content with merged profiles
                toml_content = ConfigurationManager.generate_toml_content_multi_profile(merged_profile_data)
                
            except Exception as e:
                self.log.warning(f"Failed to parse existing config file: {str(e)}, creating new one")
                # Fall back to creating a new config file
                config = ConfigurationManager.get_defaults_with_dll_detection(dll_service)
                toml_content = ConfigurationManager.generate_toml_content(config)
        else:
            # No existing config file, create a new one with defaults
            config = ConfigurationManager.get_defaults_with_dll_detection(dll_service)
            toml_content = ConfigurationManager.generate_toml_content(config)
            self.log.info(f"Creating new config file")
        
        # Write config file
        self._write_file(self.config_file_path, toml_content, 0o644)
        self.log.info(f"Created config file at {self.config_file_path}")
        
        # Log detected DLL path if found - USE GENERATED CONSTANTS
        from .config_schema_generated import DLL
        try:
            # Try to parse the written content to get the DLL path
            final_content = self.config_file_path.read_text(encoding='utf-8')
            final_config = ConfigurationManager.parse_toml_content(final_content)
            if final_config.get(DLL):
                self.log.info(f"Configured DLL path: {final_config[DLL]}")
        except (OSError, IOError, ValueError, KeyError) as e:
            # Don't fail installation if we can't log the DLL path
            self.log.debug(f"Could not log DLL path: {e}")
    
    def _create_lsfg_launch_script(self) -> None:
        """Create the ~/lsfg launch script for easier game setup"""
        from .configuration import ConfigurationService

        profile_data = ConfigurationManager.parse_toml_content_multi_profile(
            self.config_file_path.read_text(encoding="utf-8")
        )
        config_service = ConfigurationService(logger=self.log)
        config_service.user_home = self.user_home
        config_service.lsfg_script_path = self.lsfg_launch_script_path
        config_service.config_file_path = self.config_file_path

        script_content = config_service._generate_script_content_for_profile(profile_data)

        self._write_file(self.lsfg_launch_script_path, script_content, 0o755)
        self.log.info(f"Created lsfg launch script at {self.lsfg_launch_script_path}")
    
    def get_launch_script_path(self) -> str:
        """Get the path to the lsfg launch script
        
        Returns:
            String path to the launch script file
        """
        return str(self.lsfg_launch_script_path)

    def check_installation(self) -> InstallationCheckResponse:
        """Check if lsfg-vk is already installed
        
        Returns:
            InstallationCheckResponse with installation status and file paths
        """
        try:
            lib_exists = self.lib_file.exists()
            json_exists = self.json_file.exists()
            config_exists = self.config_file_path.exists()
            
            self.log.info(f"Installation check: lib={lib_exists}, json={json_exists}, config={config_exists}")
            
            return {
                "installed": lib_exists and json_exists,
                "lib_exists": lib_exists,
                "json_exists": json_exists,
                "script_exists": config_exists,  # Keep script_exists for backward compatibility
                "lib_path": str(self.lib_file),
                "json_path": str(self.json_file),
                "script_path": str(self.config_file_path),  # Keep script_path for backward compatibility
                "error": None
            }
            
        except Exception as e:
            error_msg = f"Error checking lsfg-vk installation: {str(e)}"
            self.log.error(error_msg)
            return {
                "installed": False,
                "lib_exists": False,
                "json_exists": False,
                "script_exists": False,
                "lib_path": str(self.lib_file),
                "json_path": str(self.json_file),
                "script_path": str(self.config_file_path),
                "error": str(e)
            }
    
    def uninstall(self) -> UninstallationResponse:
        """Uninstall lsfg-vk by removing the installed files
        
        Note: The config file (conf.toml) is preserved to maintain user's custom profiles
        
        Returns:
            UninstallationResponse with success status and removed files list
        """
        try:
            removed_files = []
            # Remove core lsfg-vk files, but preserve config file to maintain user's custom profiles
            files_to_remove = [
                self.lib_file,
                self.json_file,
                self.cli_file,
                self.lsfg_launch_script_path
            ]
            
            for file_path in files_to_remove:
                if self._remove_if_exists(file_path):
                    removed_files.append(str(file_path))
            
            # Also try to remove the old script file if it exists (for backward compatibility)
            if self._remove_if_exists(self.lsfg_script_path):
                removed_files.append(str(self.lsfg_script_path))
            
            # Don't remove config directory since we're preserving the config file
            
            if not removed_files:
                return self._success_response(UninstallationResponse,
                                            "No lsfg-vk files found to remove",
                                            removed_files=None)
            
            self.log.info("lsfg-vk uninstalled successfully")
            return self._success_response(UninstallationResponse, 
                                        f"lsfg-vk uninstalled successfully. Removed {len(removed_files)} files.",
                                        removed_files=removed_files)
            
        except OSError as e:
            error_msg = f"Error uninstalling lsfg-vk: {str(e)}"
            self.log.error(error_msg)
            return self._error_response(UninstallationResponse, str(e), 
                                      message="", removed_files=None)
    
    def cleanup_on_uninstall(self) -> None:
        """Clean up lsfg-vk files when the plugin is uninstalled
        
        Note: The config file (conf.toml) is preserved to maintain user's custom profiles
        """
        try:
            self.log.info("Checking for lsfg-vk files to clean up:")
            self.log.info(f"  Library file: {self.lib_file}")
            self.log.info(f"  JSON file: {self.json_file}")
            self.log.info(f"  Config file: {self.config_file_path} (preserved)")
            self.log.info(f"  Launch script: {self.lsfg_launch_script_path}")
            self.log.info(f"  Old script file: {self.lsfg_script_path}")
            
            removed_files = []
            # Remove core lsfg-vk files, but preserve config file to maintain user's custom profiles
            files_to_remove = [
                self.lib_file,
                self.json_file,
                self.cli_file,
                self.lsfg_launch_script_path,
                self.lsfg_script_path
            ]
            
            for file_path in files_to_remove:
                try:
                    if self._remove_if_exists(file_path):
                        removed_files.append(str(file_path))
                except OSError as e:
                    self.log.error(f"Failed to remove {file_path}: {e}")
            
            # Don't remove config directory since we're preserving the config file
            
            if removed_files:
                self.log.info(f"Cleaned up {len(removed_files)} lsfg-vk files during plugin uninstall: {removed_files}")
            else:
                self.log.info("No lsfg-vk files found to clean up during plugin uninstall")
                
        except Exception as e:
            self.log.error(f"Error cleaning up lsfg-vk files during uninstall: {str(e)}")
            self.log.error(f"Traceback: {traceback.format_exc()}")

    def _merge_config_with_defaults(self, existing_profile_data, dll_service):
        """Merge existing user config with current schema defaults
        
        This ensures that:
        1. User's custom profiles and values are preserved
        2. Any new fields added to the schema get their default values
        3. Global settings like DLL path are updated as needed
        
        Args:
            existing_profile_data: The user's existing ProfileData
            dll_service: DLL detection service for updating DLL path
            
        Returns:
            ProfileData with merged configuration
        """
        from .config_schema import ProfileData
        
        # Get current schema defaults
        default_config = ConfigurationManager.get_defaults_with_dll_detection(dll_service)
        default_global_config = {
            "dll": default_config.get("dll", ""),
            "no_fp16": False
        }
        
        # Start with existing data
        merged_data: ProfileData = {
            "current_profile": existing_profile_data.get("current_profile", "decky-lsfg-vk"),
            "global_config": existing_profile_data.get("global_config", {}).copy(),
            "profiles": {}
        }
        
        # Merge global config: preserve user values, add missing fields, update DLL
        for key, default_value in default_global_config.items():
            if key not in merged_data["global_config"]:
                merged_data["global_config"][key] = default_value
                self.log.info(f"Added missing global field '{key}' with default value: {default_value}")
        
        # Update DLL path if detected
        dll_result = dll_service.check_lossless_scaling_dll()
        if dll_result.get("detected") and dll_result.get("path"):
            old_dll = merged_data["global_config"].get("dll")
            merged_data["global_config"]["dll"] = dll_result["path"]
            if old_dll != dll_result["path"]:
                self.log.info(f"Updated DLL path from '{old_dll}' to: {dll_result['path']}")
        
        # Merge each profile: preserve user values, add missing fields
        existing_profiles = existing_profile_data.get("profiles", {})
        
        for profile_name, existing_profile_config in existing_profiles.items():
            merged_profile_config = existing_profile_config.copy()
            
            # Add any missing fields from current schema with default values
            added_fields = []
            for key, default_value in default_config.items():
                if key not in merged_profile_config and key not in ["dll", "no_fp16"]:  # Skip global fields
                    merged_profile_config[key] = default_value
                    added_fields.append(key)
            
            if added_fields:
                self.log.info(f"Profile '{profile_name}': Added missing fields {added_fields}")
            
            merged_data["profiles"][profile_name] = merged_profile_config
        
        # If no profiles exist, create the default one
        if not merged_data["profiles"]:
            merged_data["profiles"]["decky-lsfg-vk"] = {
                k: v for k, v in default_config.items() 
                if k not in ["dll", "no_fp16"]  # Exclude global fields
            }
            merged_data["current_profile"] = "decky-lsfg-vk"
            self.log.info("No existing profiles found, created default profile")
        
        return merged_data
