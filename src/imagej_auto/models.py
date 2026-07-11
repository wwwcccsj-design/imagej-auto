from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ImageTriplet:
    group: str
    red: Path
    green: Path
    merge: Path


@dataclass(frozen=True)
class RawMeasurement:
    group: str
    red_image: str
    green_image: str
    merge_image: str
    red_threshold_otsu: int
    green_threshold_otsu: int
    red_positive_pixels: int
    green_positive_pixels: int
    red_mean_positive_intensity: float
    green_mean_positive_intensity: float
    roi_label: str = ""
    roi_x: int = 0
    roi_y: int = 0
    roi_radius: int = 0
    roi_area: float = 0.0
    red_raw_integrated_density: float = 0.0
    red_positive_intensity_sum: float = float("nan")
    red_background_mean: float = 0.0
    red_corrected_intensity_raw: float = 0.0
    green_raw_integrated_density: float = 0.0
    green_positive_intensity_sum: float = float("nan")
    green_background_mean: float = 0.0
    green_corrected_intensity_raw: float = 0.0
    red_saturated_pixel_percent: float = 0.0
    green_saturated_pixel_percent: float = 0.0
    saturated_pixel_percent: float = 0.0


@dataclass(frozen=True)
class CalculatedMeasurement:
    group: str
    red_positive_pixels: int
    green_positive_pixels: int
    total_positive_pixels: int
    red_green_ratio: float
    dead_percent: float
    live_percent: float
    pi_threshold: int
    calcein_threshold: int
    red_image: str
    green_image: str
    merge_image: str
    red_mean_positive_intensity: float
    green_mean_positive_intensity: float
    roi_label: str = ""
    roi_x: int = 0
    roi_y: int = 0
    roi_radius: int = 0
    roi_area: float = 0.0
    red_raw_integrated_density: float = 0.0
    red_positive_intensity_sum: float = float("nan")
    red_background_mean: float = 0.0
    red_corrected_intensity_raw: float = 0.0
    red_corrected_intensity: float = 0.0
    green_raw_integrated_density: float = 0.0
    green_positive_intensity_sum: float = float("nan")
    green_background_mean: float = 0.0
    green_corrected_intensity_raw: float = 0.0
    green_corrected_intensity: float = 0.0
    red_saturated_pixel_percent: float = 0.0
    green_saturated_pixel_percent: float = 0.0
    saturated_pixel_percent: float = 0.0
    calculation_status: str = "OK"
    qc_warning: str = ""


@dataclass(frozen=True)
class RepeatSummary:
    group: str
    replicate: str
    valid_roi_count: int
    red_corrected_mean: float
    green_corrected_mean: float
    red_green_ratio: float
    dead_percent: float
    live_percent: float
    has_qc_warning: bool = False


@dataclass(frozen=True)
class ReplicateSummary:
    group: str
    replicate_count: int
    red_green_ratio_mean: float
    red_green_ratio_sd: float
    dead_percent_mean: float
    dead_percent_sd: float
    live_percent_mean: float
    live_percent_sd: float


@dataclass(frozen=True)
class TrendPoint:
    group: str
    value: float


@dataclass(frozen=True)
class TrendCheck:
    expected_trend: str
    selected_label: str
    metric_label: str
    direction_label: str
    passed: bool
    points: list[TrendPoint]
    message: str


@dataclass(frozen=True)
class PipelineOptions:
    threshold_method: str = "Otsu"
    min_threshold: int = 8
    order: tuple[str, str, str] = ("red", "green", "merge")
    group_names: list[str] = field(default_factory=list)
    replicates_per_group: int = 3
    roi_per_replicate: int = 3
    expected_trend: str = "none"
    trend_min_value: float | None = None
    trend_max_value: float | None = None
    image_layout: str = "auto"
    threshold_scope: str = "fixed"
    red_fixed_threshold: int = 80
    green_fixed_threshold: int = 80
    use_min_threshold: bool = True
    background_subtraction: bool = True
    show_threshold_preview: bool = False
    roi_mode: str = "fixed_circle"
    dead_percent_mode: str = "intensity_ratio"
    enable_watershed: bool = False
    min_particle_size: float = 20.0
    max_particle_size: float = 100000.0
    circularity_min: float = 0.0
    circularity_max: float = 1.0
    exclude_edge_objects: bool = True
    overlap_mode: str = "union"
    fiji_path: str = ""
