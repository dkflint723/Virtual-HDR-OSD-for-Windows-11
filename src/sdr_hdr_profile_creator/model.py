from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any, Literal

DisplayMode = Literal["SDR", "HDR"]


@dataclass(slots=True)
class ModeState:
    profile_name: str

    # HDR corrections. The UI keeps generous ranges with fine-grained steps for precise calibration.
    temperature: float = 0.0          # Kelvin offset from D65; + warms, - cools.
    red_channel: float = 0.0          # Percent gain trim around the neutral axis.
    green_channel: float = 0.0
    blue_channel: float = 0.0
    tint: float = 0.0                 # Fine green <-> magenta trim (app units).
    gamma: float = 2.2                # Traditional gamma; 2.20 is neutral.
    saturation: float = 0.0           # Percent chroma trim.
    brightness_trim: float = 0.0      # Subtle midtone brightness trim, percent.
    contrast: float = 0.0             # Subtle contrast trim, percent.

    # Legacy field retained only so older embedded profile states deserialize safely.
    # It is no longer exposed in the GUI and never affects the generated HDR transform.
    brightness: float = 30.0

    # Retained only so older embedded states deserialize harmlessly. These controls
    # are not exposed and are always neutralized on import.
    gamma_conversion: str = "None"
    sdr_gamma_correction: str = "Off"
    exposure: float = 0.0
    low_lights: float = 0.0
    mid_lights: float = 0.0
    high_lights: float = 0.0

    minimum_luminance_nits: float = 0.0
    peak_luminance_nits: float = 1000.0
    full_frame_luminance_nits: float = 400.0
    lut_entries: int = 4096
    imported_profile: str = ""
    base_profile: str = ""
    base_profile_name: str = ""

    @classmethod
    def neutral(cls, mode: DisplayMode) -> "ModeState":
        if mode == "SDR":
            return cls(
                profile_name="Neutral SDR",
                brightness=30.0,
                peak_luminance_nits=100.0,
                full_frame_luminance_nits=100.0,
            )
        return cls(
            profile_name="Virtual HDR OSD",
            brightness=30.0,
            peak_luminance_nits=1000.0,
            full_frame_luminance_nits=400.0,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any], fallback_mode: DisplayMode) -> "ModeState":
        base = cls.neutral(fallback_mode)
        merged = base.to_dict()
        allowed = {f.name for f in fields(cls)}
        merged.update({k: v for k, v in data.items() if k in allowed})

        # Older development profiles stored Gamma as a log2 trim around 0. Convert only
        # that legacy representation; current profiles store an explicit gamma value.
        if "gamma" in data and "gamma_fix_enabled" not in data:
            try:
                legacy_gamma = float(data["gamma"])
                if -1.5 <= legacy_gamma <= 1.5:
                    merged["gamma"] = 2.2 * (2.0 ** legacy_gamma)
            except (TypeError, ValueError):
                pass

        limits = {
            "temperature": (-3000.0, 3000.0),
            "red_channel": (-25.0, 25.0),
            "green_channel": (-25.0, 25.0),
            "blue_channel": (-25.0, 25.0),
            "tint": (-25.0, 25.0),
            "gamma": (1.6, 3.0),
            "saturation": (-50.0, 50.0),
            "brightness_trim": (-30.0, 30.0),
            "contrast": (-30.0, 30.0),
            "brightness": (0.0, 100.0),
            "minimum_luminance_nits": (0.0, 100.0),
            "peak_luminance_nits": (80.0, 10000.0),
            "full_frame_luminance_nits": (80.0, 10000.0),
        }
        for key, (low, high) in limits.items():
            try:
                value = float(merged[key])
            except Exception:
                value = float(getattr(base, key))
            merged[key] = max(low, min(high, value))

        # Removed controls are neutralized when legacy profiles are imported.
        merged["exposure"] = 0.0
        merged["low_lights"] = 0.0
        merged["mid_lights"] = 0.0
        merged["high_lights"] = 0.0
        merged["gamma_conversion"] = "None"
        allowed_corrections = {"Off", "Auto (Recommended)", "100 nits / Brightness 5", "200 nits / Brightness 30", "300 nits / Brightness 55", "400 nits / Brightness 80", "Unspecified", "SDR"}
        merged["sdr_gamma_correction"] = str(merged.get("sdr_gamma_correction", "Off"))
        if merged["sdr_gamma_correction"] not in allowed_corrections:
            merged["sdr_gamma_correction"] = "Off"

        merged["minimum_luminance_nits"] = min(
            merged["minimum_luminance_nits"], merged["peak_luminance_nits"]
        )
        merged["full_frame_luminance_nits"] = min(
            merged["full_frame_luminance_nits"], merged["peak_luminance_nits"]
        )
        merged["profile_name"] = str(merged.get("profile_name") or base.profile_name)[:160]
        merged["imported_profile"] = str(merged.get("imported_profile", ""))
        merged["base_profile"] = str(merged.get("base_profile", ""))
        merged["base_profile_name"] = str(merged.get("base_profile_name", ""))[:240]
        try:
            merged["lut_entries"] = max(256, min(4096, int(merged["lut_entries"])))
        except Exception:
            merged["lut_entries"] = 4096
        return cls(**merged)


@dataclass(slots=True)
class ApplicationState:
    current_mode: DisplayMode
    follow_windows_mode: bool
    auto_refresh_after_mode_change: bool
    live_mode: bool
    selected_display_key: str
    sdr: ModeState
    hdr: ModeState

    @classmethod
    def neutral(cls) -> "ApplicationState":
        return cls(
            "HDR",
            True,
            True,
            False,
            "",
            ModeState.neutral("SDR"),
            ModeState.neutral("HDR"),
        )

    def mode_state(self, mode: DisplayMode | None = None) -> ModeState:
        return self.sdr if (mode or self.current_mode) == "SDR" else self.hdr

    def set_mode_state(self, mode: DisplayMode, state: ModeState) -> None:
        if mode == "SDR":
            self.sdr = state
        else:
            self.hdr = state

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "virtual-hdr-osd-state-v1",
            "current_mode": self.current_mode,
            "follow_windows_mode": self.follow_windows_mode,
            "auto_refresh_after_mode_change": self.auto_refresh_after_mode_change,
            "live_mode": self.live_mode,
            "selected_display_key": self.selected_display_key,
            "sdr": self.sdr.to_dict(),
            "hdr": self.hdr.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ApplicationState":
        return cls(
            "HDR" if data.get("current_mode") != "SDR" else "SDR",
            bool(data.get("follow_windows_mode", True)),
            bool(data.get("auto_refresh_after_mode_change", True)),
            False,
            str(data.get("selected_display_key", "")),
            ModeState.from_dict(dict(data.get("sdr", {})), "SDR"),
            ModeState.from_dict(dict(data.get("hdr", {})), "HDR"),
        )
