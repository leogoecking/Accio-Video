"""
WebUI Settings Transfer & Key Backup Utilities
"""
import json
from app.models.schema import VideoParams

SETTINGS_PRESET_SCHEMA = "accio-video.settings-preset"
SETTINGS_PRESET_VERSION = 1
SETTINGS_PRESET_FILE_NAME = "accio-video-settings.json"
KEY_BACKUP_SCHEMA = "accio-video.key-backup"
KEY_BACKUP_VERSION = 1
KEY_BACKUP_FILE_NAME = "accio-video-key-backup.json"

PRESET_EXCLUDED_PARAM_KEYS = frozenset(
    {
        "custom_audio_file",
        "custom_bgm_file",
        "material_directory",
    }
)
CREDENTIAL_KEY_SUFFIXES = ("_api_key", "_api_keys", "_token", "_token_url", "_secret")
CREDENTIAL_COMPANION_KEYS = {
    "azure": ("speech_region",),
    "chatterbox": ("base_url", "model_id", "voices"),
    "cloudflare": ("account_id", "gateway_id", "model_name"),
    "elevenlabs": ("model_id", "music_model_id"),
    "fish_audio": ("model", "voices"),
    "minimax_tts": (
        "base_url",
        "model_id",
        "voice_id",
        "sample_rate",
        "bitrate",
        "audio_format",
        "channel",
        "pitch",
    ),
    "app": (
        "endpoint",
        "script_generation_backend",
        "loomloom_base_url",
        "upload_post_username",
        "upload_post_platforms",
        "upload_post_auto_upload",
        "upload_post_youtube_privacy_status",
    ),
}
CREDENTIAL_WIDGET_STATE_ALIASES = {
    ("azure", "speech_key"): ("azure_speech_key_input",),
    ("azure", "speech_region"): ("azure_speech_region_input",),
    ("app", "loomloom_api_token"): ("loomloom_token_input",),
    ("app", "loomloom_base_url"): ("loomloom_url_input",),
}
KEY_BACKUP_EXCLUDED_SECTIONS = frozenset({"ui"})


def is_credential_config_key(key: str) -> bool:
    return any(key.endswith(suffix) for suffix in CREDENTIAL_KEY_SUFFIXES)


def is_backup_config_key(section_name: str, key: str) -> bool:
    if is_credential_config_key(key):
        return True
    return key in CREDENTIAL_COMPANION_KEYS.get(section_name, ())


def credential_widget_state_keys(section_name: str, key: str) -> tuple[str, ...]:
    if section_name == "app":
        default_widget_key = f"{key}_input"
    else:
        default_widget_key = f"{section_name}_{key}_input"
    return (
        default_widget_key,
        *CREDENTIAL_WIDGET_STATE_ALIASES.get((section_name, key), ()),
    )


def normalize_backup_value(value):
    if isinstance(value, list):
        items = [
            str(item).strip()
            for item in value
            if isinstance(item, (str, int, float)) and str(item).strip()
        ]
        return items or None
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        text = str(value).strip()
        return text or None
    return None


def collect_key_backup(config_sections):
    backup = {}
    for section_name, section in config_sections.items():
        if section_name in KEY_BACKUP_EXCLUDED_SECTIONS:
            continue
        entries = {}
        for key, value in section.items():
            if not is_backup_config_key(section_name, key):
                continue
            normalized_value = normalize_backup_value(value)
            if normalized_value is not None:
                entries[key] = normalized_value
        if entries:
            backup[section_name] = entries
    return backup


def count_backup_keys(backup: dict) -> int:
    return sum(len(entries) for entries in backup.values())


def build_key_backup_payload(config_sections, app_version: str) -> dict:
    return {
        "schema": KEY_BACKUP_SCHEMA,
        "version": KEY_BACKUP_VERSION,
        "app_version": str(app_version),
        "keys": collect_key_backup(config_sections),
    }


def load_transfer_payload(raw_bytes: bytes, schema: str, version: int) -> dict:
    payload = json.loads(raw_bytes.decode("utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("exported file must contain a JSON object")
    if payload.get("schema") != schema:
        actual_schema = payload.get("schema")
        raise ValueError(f"unexpected schema: {actual_schema!r}")
    if payload.get("version") != version:
        actual_version = payload.get("version")
        raise ValueError(f"unsupported version: {actual_version!r}")
    return payload


def parse_key_backup(raw_bytes: bytes, config_sections: dict) -> dict:
    payload = load_transfer_payload(raw_bytes, KEY_BACKUP_SCHEMA, KEY_BACKUP_VERSION)
    keys = payload.get("keys")
    if not isinstance(keys, dict):
        raise ValueError("key backup file has no keys object")

    restored = {}
    for section_name, entries in keys.items():
        if section_name not in config_sections:
            continue
        if section_name in KEY_BACKUP_EXCLUDED_SECTIONS:
            continue
        if not isinstance(entries, dict):
            continue
        section_entries = {}
        for key, value in entries.items():
            if not is_backup_config_key(section_name, key):
                continue
            normalized_value = normalize_backup_value(value)
            if normalized_value is not None:
                section_entries[key] = normalized_value
        if section_entries:
            restored[section_name] = section_entries

    if not restored:
        raise ValueError("key backup file contains no restorable keys")
    return restored


def build_settings_preset_payload(params: dict, app_version: str) -> dict:
    preset_params = {
        key: value
        for key, value in params.items()
        if key not in PRESET_EXCLUDED_PARAM_KEYS
    }
    return {
        "schema": SETTINGS_PRESET_SCHEMA,
        "version": SETTINGS_PRESET_VERSION,
        "app_version": str(app_version),
        "params": preset_params,
    }


def parse_settings_preset(raw_bytes: bytes) -> dict:
    payload = load_transfer_payload(
        raw_bytes, SETTINGS_PRESET_SCHEMA, SETTINGS_PRESET_VERSION
    )
    preset_params = payload.get("params")
    if not isinstance(preset_params, dict):
        raise ValueError("settings preset file has no params object")

    params_input = {
        key: value
        for key, value in preset_params.items()
        if key not in PRESET_EXCLUDED_PARAM_KEYS
    }
    params_input.setdefault("video_subject", "")
    return VideoParams.model_validate(params_input).model_dump(mode="json")
