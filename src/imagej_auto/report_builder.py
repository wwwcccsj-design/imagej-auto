from __future__ import annotations

import csv
import html
import json
import math
import re
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from xml.sax.saxutils import escape

from .models import CalculatedMeasurement, RawMeasurement, RepeatSummary, ReplicateSummary, TrendCheck, TrendPoint


NO_MUTATION_NOTICE = "趋势检查只用于提示，不会修改原始数据、阈值或计算结果。"


def _to_int(value: str | int | float) -> int:
    return int(float(value))


def _to_float(value: str | int | float) -> float:
    return float(value)


def _optional_float(row: dict[str, str], name: str, default: float = 0.0) -> float:
    value = row.get(name, "")
    if value == "":
        return default
    return _to_float(value)


def _optional_int(row: dict[str, str], name: str, default: int = 0) -> int:
    value = row.get(name, "")
    if value == "":
        return default
    return _to_int(value)


def parse_raw_measurements(csv_path: str | Path) -> list[RawMeasurement]:
    path = Path(csv_path)
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    def pick(row: dict[str, str], *names: str) -> str:
        for name in names:
            if name in row and row[name] != "":
                return row[name]
        raise KeyError(names[0])

    def optional(row: dict[str, str], name: str, default: str = "") -> str:
        value = row.get(name, "")
        return value if value != "" else default

    measurements = [
        RawMeasurement(
            group=row["group"],
            red_image=row["red_image"],
            green_image=row["green_image"],
            merge_image=row["merge_image"],
            red_threshold_otsu=_to_int(pick(row, "red_threshold", "red_threshold_otsu")),
            green_threshold_otsu=_to_int(pick(row, "green_threshold", "green_threshold_otsu")),
            red_positive_pixels=_to_int(row["red_positive_pixels"]),
            green_positive_pixels=_to_int(row["green_positive_pixels"]),
            red_mean_positive_intensity=_to_float(row["red_mean_positive_intensity"]),
            green_mean_positive_intensity=_to_float(row["green_mean_positive_intensity"]),
            roi_label=optional(row, "roi_label"),
            roi_x=_optional_int(row, "roi_x"),
            roi_y=_optional_int(row, "roi_y"),
            roi_radius=_optional_int(row, "roi_radius"),
            roi_area=_optional_float(row, "roi_area"),
            red_raw_integrated_density=_optional_float(row, "red_raw_integrated_density"),
            red_positive_intensity_sum=_optional_float(row, "red_positive_intensity_sum", math.nan),
            red_background_mean=_optional_float(row, "red_background_mean"),
            red_corrected_intensity_raw=_optional_float(row, "red_corrected_intensity_raw"),
            green_raw_integrated_density=_optional_float(row, "green_raw_integrated_density"),
            green_positive_intensity_sum=_optional_float(row, "green_positive_intensity_sum", math.nan),
            green_background_mean=_optional_float(row, "green_background_mean"),
            green_corrected_intensity_raw=_optional_float(row, "green_corrected_intensity_raw"),
            red_saturated_pixel_percent=_optional_float(row, "red_saturated_pixel_percent"),
            green_saturated_pixel_percent=_optional_float(row, "green_saturated_pixel_percent"),
            saturated_pixel_percent=_optional_float(
                row,
                "saturated_pixel_percent",
                max(_optional_float(row, "red_saturated_pixel_percent"), _optional_float(row, "green_saturated_pixel_percent")),
            ),
        )
        for row in rows
    ]
    if not measurements:
        raise ValueError("ImageJ 原始测量表为空。")
    return measurements


def _fallback_area(raw: RawMeasurement) -> float:
    if raw.roi_area > 0:
        return raw.roi_area
    if raw.roi_radius > 0:
        return math.pi * raw.roi_radius * raw.roi_radius
    return float(max(raw.red_positive_pixels, raw.green_positive_pixels, 1))


def _fallback_integrated_density(positive_pixels: int, mean_positive_intensity: float) -> float:
    return max(0.0, float(positive_pixels) * float(mean_positive_intensity))


def _is_provided(value: float) -> bool:
    return not (isinstance(value, float) and math.isnan(value))


def _corrected_intensity_from_raw(
    corrected_raw: float,
    positive_intensity_sum: float,
    raw_integrated: float,
    roi_area: float,
    background_mean: float,
) -> float:
    if _is_provided(positive_intensity_sum):
        return corrected_raw
    if corrected_raw != 0:
        return corrected_raw
    return raw_integrated - roi_area * background_mean


def calculate_measurement(raw: RawMeasurement) -> CalculatedMeasurement:
    roi_area = _fallback_area(raw)
    red_raw = raw.red_raw_integrated_density or _fallback_integrated_density(
        raw.red_positive_pixels,
        raw.red_mean_positive_intensity,
    )
    green_raw = raw.green_raw_integrated_density or _fallback_integrated_density(
        raw.green_positive_pixels,
        raw.green_mean_positive_intensity,
    )
    red_corrected_raw = _corrected_intensity_from_raw(
        raw.red_corrected_intensity_raw,
        raw.red_positive_intensity_sum,
        red_raw,
        roi_area,
        raw.red_background_mean,
    )
    green_corrected_raw = _corrected_intensity_from_raw(
        raw.green_corrected_intensity_raw,
        raw.green_positive_intensity_sum,
        green_raw,
        roi_area,
        raw.green_background_mean,
    )
    red_corrected = max(0.0, red_corrected_raw)
    green_corrected = max(0.0, green_corrected_raw)
    total_corrected = red_corrected + green_corrected

    warnings: list[str] = []
    if red_corrected_raw < 0:
        warnings.append("红色通道背景扣除后为负值，已在计算中按0处理")
    if green_corrected_raw < 0:
        warnings.append("绿色通道背景扣除后为负值，已在计算中按0处理")
    if total_corrected <= 0:
        warnings.append("红绿校正信号均为0，无法计算Dead %")
    elif green_corrected <= 0 < red_corrected:
        warnings.append("绿色校正信号为0，Red/Green无法计算")
    saturated = max(raw.saturated_pixel_percent, raw.red_saturated_pixel_percent, raw.green_saturated_pixel_percent)
    if saturated > 1:
        warnings.append(f"饱和像素比例较高({saturated:.2f}%)")

    if red_corrected > 0 and green_corrected > 0:
        red_green_ratio = red_corrected / green_corrected
    elif red_corrected > 0 and green_corrected == 0:
        red_green_ratio = math.inf
    elif red_corrected == 0 and green_corrected > 0:
        red_green_ratio = 0.0
    else:
        red_green_ratio = math.nan

    if total_corrected > 0:
        dead_percent = red_corrected / total_corrected * 100
        live_percent = 100 - dead_percent
    else:
        dead_percent = math.nan
        live_percent = math.nan

    return CalculatedMeasurement(
        group=raw.group,
        red_positive_pixels=raw.red_positive_pixels,
        green_positive_pixels=raw.green_positive_pixels,
        total_positive_pixels=raw.red_positive_pixels + raw.green_positive_pixels,
        red_green_ratio=red_green_ratio,
        dead_percent=dead_percent,
        live_percent=live_percent,
        pi_threshold=raw.red_threshold_otsu,
        calcein_threshold=raw.green_threshold_otsu,
        red_image=raw.red_image,
        green_image=raw.green_image,
        merge_image=raw.merge_image,
        red_mean_positive_intensity=raw.red_mean_positive_intensity,
        green_mean_positive_intensity=raw.green_mean_positive_intensity,
        roi_label=raw.roi_label,
        roi_x=raw.roi_x,
        roi_y=raw.roi_y,
        roi_radius=raw.roi_radius,
        roi_area=roi_area,
        red_raw_integrated_density=red_raw,
        red_positive_intensity_sum=raw.red_positive_intensity_sum,
        red_background_mean=raw.red_background_mean,
        red_corrected_intensity_raw=red_corrected_raw,
        red_corrected_intensity=red_corrected,
        green_raw_integrated_density=green_raw,
        green_positive_intensity_sum=raw.green_positive_intensity_sum,
        green_background_mean=raw.green_background_mean,
        green_corrected_intensity_raw=green_corrected_raw,
        green_corrected_intensity=green_corrected,
        red_saturated_pixel_percent=raw.red_saturated_pixel_percent,
        green_saturated_pixel_percent=raw.green_saturated_pixel_percent,
        saturated_pixel_percent=saturated,
        calculation_status="OK" if not warnings else "QC_WARNING",
        qc_warning="; ".join(warnings),
    )


def calculate_all(raw_measurements: list[RawMeasurement]) -> list[CalculatedMeasurement]:
    return [calculate_measurement(raw) for raw in raw_measurements]


def _split_group_repeat(group: str) -> tuple[str, str]:
    match = re.fullmatch(r"(.+)\s+(R\d+)", group)
    if not match:
        return group, "R1"
    return match.group(1), match.group(2)


def _base_replicate_group(group: str) -> str:
    return _split_group_repeat(group)[0]


def _mean(values: list[float]) -> float:
    finite = [value for value in values if not math.isnan(value)]
    if not finite:
        return math.nan
    return sum(finite) / len(finite)


def _sample_sd(values: list[float]) -> float:
    finite = [value for value in values if not math.isnan(value)]
    if len(finite) < 2:
        return math.nan
    mean = sum(finite) / len(finite)
    variance = sum((value - mean) ** 2 for value in finite) / (len(finite) - 1)
    return math.sqrt(variance)


def summarize_repeats(calculated: list[CalculatedMeasurement]) -> list[RepeatSummary]:
    grouped: dict[tuple[str, str], list[CalculatedMeasurement]] = {}
    for row in calculated:
        group, repeat = _split_group_repeat(row.group)
        grouped.setdefault((group, repeat), []).append(row)

    summaries: list[RepeatSummary] = []
    for (group, repeat), rows in grouped.items():
        valid_rows = [
            row
            for row in rows
            if not math.isnan(row.dead_percent) and (row.red_corrected_intensity + row.green_corrected_intensity) > 0
        ]
        red_mean = _mean([row.red_corrected_intensity for row in valid_rows])
        green_mean = _mean([row.green_corrected_intensity for row in valid_rows])
        total = red_mean + green_mean if not math.isnan(red_mean) and not math.isnan(green_mean) else math.nan
        red_green_ratio = red_mean / green_mean if not math.isnan(green_mean) and green_mean > 0 else math.nan
        dead_percent = red_mean / total * 100 if not math.isnan(total) and total > 0 else math.nan
        live_percent = green_mean / total * 100 if not math.isnan(total) and total > 0 else math.nan
        summaries.append(
            RepeatSummary(
                group=group,
                replicate=repeat,
                valid_roi_count=len(valid_rows),
                red_corrected_mean=red_mean,
                green_corrected_mean=green_mean,
                red_green_ratio=red_green_ratio,
                dead_percent=dead_percent,
                live_percent=live_percent,
                has_qc_warning=any(row.qc_warning for row in rows) or len(valid_rows) != len(rows),
            )
        )
    return summaries


def summarize_replicates(calculated: list[CalculatedMeasurement]) -> list[ReplicateSummary]:
    repeat_summaries = summarize_repeats(calculated)
    grouped: dict[str, list[RepeatSummary]] = {}
    for row in repeat_summaries:
        grouped.setdefault(row.group, []).append(row)

    summaries: list[ReplicateSummary] = []
    for group, rows in grouped.items():
        summaries.append(
            ReplicateSummary(
                group=group,
                replicate_count=len(rows),
                red_green_ratio_mean=_mean([row.red_green_ratio for row in rows]),
                red_green_ratio_sd=_sample_sd([row.red_green_ratio for row in rows]),
                dead_percent_mean=_mean([row.dead_percent for row in rows]),
                dead_percent_sd=_sample_sd([row.dead_percent for row in rows]),
                live_percent_mean=_mean([row.live_percent for row in rows]),
                live_percent_sd=_sample_sd([row.live_percent for row in rows]),
            )
        )
    return summaries


TREND_OPTIONS = {
    "none": ("不进行趋势判断", "", ""),
    "pbmc_dead_increase": ("按PBMC状态分别判断Dead %递增", "Dead %", "递增"),
    "pbmc_dead_decrease": ("按PBMC状态分别判断Dead %递减", "Dead %", "递减"),
    "custom": ("自定义组内顺序", "Dead %", "提示"),
}


def describe_expected_trend(expected_trend: str) -> str:
    if expected_trend not in TREND_OPTIONS:
        raise ValueError("预期趋势选项无效。")
    return TREND_OPTIONS[expected_trend][0]


def _dose_rank(group: str) -> int | None:
    text = group.lower().replace(" ", "").replace("μ", "u")
    if text.startswith("con") and "aur" not in text:
        return 0
    if "aur2" in text or "aur(2" in text or "2um" in text or "2u" in text:
        return 1
    if "aur4" in text or "aur(4" in text or "4um" in text or "4u" in text:
        return 2
    return None


def _pbmc_bucket(group: str) -> str:
    return "PBMC" if "pbmc" in group.lower() else "无PBMC"


def _ordered_pbmc_points(summaries: list[ReplicateSummary]) -> dict[str, list[TrendPoint]]:
    buckets: dict[str, list[tuple[int, TrendPoint]]] = {"无PBMC": [], "PBMC": []}
    for row in summaries:
        rank = _dose_rank(row.group)
        if rank is None:
            continue
        buckets[_pbmc_bucket(row.group)].append((rank, TrendPoint(row.group, row.dead_percent_mean)))
    return {bucket: [point for _, point in sorted(points, key=lambda item: item[0])] for bucket, points in buckets.items()}


def _monotonic(values: list[float], increasing: bool) -> bool:
    if any(math.isnan(value) for value in values):
        return False
    pairs = zip(values, values[1:])
    if increasing:
        return all(left <= right + 1e-12 for left, right in pairs)
    return all(left + 1e-12 >= right for left, right in pairs)


def _direction_word(left: float, right: float) -> str:
    if math.isnan(left) or math.isnan(right):
        return "无法比较"
    if right > left + 1e-12:
        return "升高"
    if right + 1e-12 < left:
        return "降低"
    return "持平"


def _pbmc_segment_message(bucket: str, points: list[TrendPoint], increasing: bool) -> tuple[bool, str]:
    if len(points) < 2:
        return True, f"{bucket}: 可比较组不足2个，跳过。"
    values = [point.value for point in points]
    passed = _monotonic(values, increasing)
    joined = " -> ".join(f"{point.group}: {point.value:.2f}" if not math.isnan(point.value) else f"{point.group}: NA" for point in points)
    status = "符合" if passed else "不符合"
    detail = ""
    if len(points) >= 3:
        detail = (
            f"；第2组较对照{_direction_word(values[0], values[1])}"
            f"，第3组较第2组{_direction_word(values[1], values[2])}"
            f"，第3组较对照{_direction_word(values[0], values[2])}"
        )
    direction = "递增" if increasing else "递减"
    return passed, f"{bucket}: {joined}；{status}{direction}{detail}。"


def _apply_trend_bounds(
    check: TrendCheck,
    summaries: list[ReplicateSummary],
    min_value: float | None,
    max_value: float | None,
) -> TrendCheck:
    if min_value is None and max_value is None:
        return check
    if min_value is not None and not 0 <= min_value <= 100:
        raise ValueError("趋势最小值必须在0到100之间。")
    if max_value is not None and not 0 <= max_value <= 100:
        raise ValueError("趋势最大值必须在0到100之间。")
    if min_value is not None and max_value is not None and min_value > max_value:
        raise ValueError("趋势最小值不能大于最大值。")

    range_points = [TrendPoint(row.group, row.dead_percent_mean) for row in summaries]
    violations: list[str] = []
    for point in range_points:
        if math.isnan(point.value):
            violations.append(f"{point.group}=NA")
        elif min_value is not None and point.value < min_value:
            violations.append(f"{point.group}={point.value:.2f}<最小值{min_value:.2f}")
        elif max_value is not None and point.value > max_value:
            violations.append(f"{point.group}={point.value:.2f}>最大值{max_value:.2f}")
    bounds = f"[{min_value if min_value is not None else '-∞'}, {max_value if max_value is not None else '+∞'}]"
    range_passed = not violations
    range_message = (
        f"Dead %范围检查{bounds}通过。"
        if range_passed
        else f"Dead %范围检查{bounds}未通过：" + "；".join(violations) + "。"
    )
    return TrendCheck(
        check.expected_trend,
        check.selected_label,
        check.metric_label or "Dead %",
        check.direction_label,
        check.passed and range_passed,
        check.points or range_points,
        f"{check.message} {range_message} 范围检查仅用于提示，不会优化、裁剪或修改数据。",
    )


def check_expected_trend(
    summaries: list[ReplicateSummary],
    expected_trend: str,
    min_value: float | None = None,
    max_value: float | None = None,
) -> TrendCheck:
    if expected_trend not in TREND_OPTIONS:
        raise ValueError("预期趋势选项无效。")
    selected_label, metric_label, direction_label = TREND_OPTIONS[expected_trend]
    if expected_trend == "none":
        check = TrendCheck(
            expected_trend,
            selected_label,
            metric_label,
            direction_label,
            True,
            [],
            f"用户选择：{selected_label}；未进行趋势判断。{NO_MUTATION_NOTICE}",
        )
    elif expected_trend == "custom":
        points = [TrendPoint(row.group, row.dead_percent_mean) for row in summaries]
        joined = " -> ".join(f"{point.group}: {point.value:.2f}" if not math.isnan(point.value) else f"{point.group}: NA" for point in points)
        check = TrendCheck(
            expected_trend,
            selected_label,
            metric_label,
            direction_label,
            True,
            points,
            f"用户选择：{selected_label}；按当前分组输入顺序仅显示Dead %：{joined}。{NO_MUTATION_NOTICE}",
        )
    else:
        increasing = expected_trend == "pbmc_dead_increase"
        buckets = _ordered_pbmc_points(summaries)
        messages: list[str] = []
        passed_values: list[bool] = []
        points = []
        for bucket in ("无PBMC", "PBMC"):
            segment_points = buckets[bucket]
            points.extend(segment_points)
            passed, message = _pbmc_segment_message(bucket, segment_points, increasing)
            passed_values.append(passed)
            messages.append(message)

        check = TrendCheck(
            expected_trend,
            selected_label,
            metric_label,
            "递增" if increasing else "递减",
            all(passed_values),
            points,
            f"用户选择：{selected_label}；" + " ".join(messages) + f" {NO_MUTATION_NOTICE}",
        )
    return _apply_trend_bounds(check, summaries, min_value, max_value)


def _fmt(value: float, digits: int = 6) -> str:
    if isinstance(value, float) and math.isnan(value):
        return ""
    if isinstance(value, float) and math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    return f"{value:.{digits}f}"


def write_csv(path: str | Path, headers: list[str], rows: list[dict[str, object]]) -> None:
    with Path(path).open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def write_prism_csv_with_mean_appendix(
    path: str | Path,
    headers: list[str],
    rows: list[dict[str, object]],
    appendix_headers: list[str],
    appendix_rows: list[dict[str, object]],
) -> None:
    with Path(path).open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)
        f.write("\n")
        csv.writer(f).writerow(["三组重复平均值"])
        appendix_writer = csv.DictWriter(f, fieldnames=appendix_headers)
        appendix_writer.writeheader()
        appendix_writer.writerows(appendix_rows)


def write_roi_level_csv(calculated: list[CalculatedMeasurement], out_dir: str | Path, filename: str = "roi_level_results.csv") -> Path:
    out = Path(out_dir) / filename
    rows = []
    for row in calculated:
        group, repeat = _split_group_repeat(row.group)
        rows.append(
            {
                "Group": group,
                "Biological_repeat": repeat,
                "ROI_number": row.roi_label,
                "ROI_label": row.roi_label,
                "Original_group_label": row.group,
                "ROI_x": row.roi_x,
                "ROI_y": row.roi_y,
                "ROI_radius": row.roi_radius,
                "ROI_area": _fmt(row.roi_area),
                "Red_threshold": row.pi_threshold,
                "Green_threshold": row.calcein_threshold,
                "Red_raw_integrated": _fmt(row.red_raw_integrated_density),
                "Red_positive_pixel_count": row.red_positive_pixels,
                "Red_positive_intensity_sum": _fmt(row.red_positive_intensity_sum),
                "Red_background_mean": _fmt(row.red_background_mean),
                "Red_corrected": _fmt(row.red_corrected_intensity),
                "Green_raw_integrated": _fmt(row.green_raw_integrated_density),
                "Green_positive_pixel_count": row.green_positive_pixels,
                "Green_positive_intensity_sum": _fmt(row.green_positive_intensity_sum),
                "Green_background_mean": _fmt(row.green_background_mean),
                "Green_corrected": _fmt(row.green_corrected_intensity),
                "Red_corrected_intensity": _fmt(row.red_corrected_intensity),
                "Green_corrected_intensity": _fmt(row.green_corrected_intensity),
                "Red_Green_ratio": _fmt(row.red_green_ratio),
                "Dead_percent": _fmt(row.dead_percent),
                "Live_percent": _fmt(row.live_percent),
                "Red_saturated_percent": _fmt(row.red_saturated_pixel_percent),
                "Green_saturated_percent": _fmt(row.green_saturated_pixel_percent),
                "PI_threshold": row.pi_threshold,
                "Calcein_threshold": row.calcein_threshold,
                "QC_status": row.calculation_status,
                "QC_warning": row.qc_warning,
            }
        )
    write_csv(out, list(rows[0].keys()) if rows else ["Group"], rows)
    return out


def write_calculated_csv(calculated: list[CalculatedMeasurement], out_dir: str | Path) -> Path:
    return write_roi_level_csv(calculated, out_dir, filename="calculated_results.csv")


def write_repeat_level_csv(repeats: list[RepeatSummary], out_dir: str | Path, filename: str = "repeat_level_results.csv") -> Path:
    out = Path(out_dir) / filename
    rows = [
        {
            "Group": row.group,
            "Biological_repeat": row.replicate,
            "Valid_ROI_count": row.valid_roi_count,
            "Red_corrected_mean": _fmt(row.red_corrected_mean),
            "Green_corrected_mean": _fmt(row.green_corrected_mean),
            "Red_Green_ratio": _fmt(row.red_green_ratio),
            "Dead_percent": _fmt(row.dead_percent),
            "Live_percent": _fmt(row.live_percent),
            "Has_QC_warning": "YES" if row.has_qc_warning else "NO",
        }
        for row in repeats
    ]
    write_csv(out, list(rows[0].keys()) if rows else ["Group"], rows)
    return out


def write_group_summary_csv(summaries: list[ReplicateSummary], out_dir: str | Path, filename: str = "group_summary.csv") -> Path:
    out = Path(out_dir) / filename
    rows = [
        {
            "Group": row.group,
            "Biological_repeats": row.replicate_count,
            "Red_Green_ratio_mean": _fmt(row.red_green_ratio_mean),
            "Red_Green_ratio_SD": _fmt(row.red_green_ratio_sd),
            "Dead_percent_mean": _fmt(row.dead_percent_mean),
            "Dead_percent_SD": _fmt(row.dead_percent_sd),
            "Live_percent_mean": _fmt(row.live_percent_mean),
            "Live_percent_SD": _fmt(row.live_percent_sd),
        }
        for row in summaries
    ]
    write_csv(out, list(rows[0].keys()) if rows else ["Group"], rows)
    return out


def write_replicate_summary_csv(summaries: list[ReplicateSummary], out_dir: str | Path) -> Path:
    return write_group_summary_csv(summaries, out_dir, filename="replicate_summary.csv")


def write_threshold_log_csv(calculated: list[CalculatedMeasurement], out_dir: str | Path) -> Path:
    out = Path(out_dir) / "threshold_log.csv"
    rows = []
    for row in calculated:
        group, repeat = _split_group_repeat(row.group)
        rows.append(
            {
                "Group": group,
                "Biological_repeat": repeat,
                "ROI_label": row.roi_label,
                "Red_image": row.red_image,
                "Green_image": row.green_image,
                "PI_threshold": row.pi_threshold,
                "Calcein_threshold": row.calcein_threshold,
                "Red_background_mean": _fmt(row.red_background_mean),
                "Green_background_mean": _fmt(row.green_background_mean),
                "Saturated_pixel_percent": _fmt(row.saturated_pixel_percent),
            }
        )
    write_csv(out, list(rows[0].keys()) if rows else ["Group"], rows)
    return out


def write_qc_report_csv(
    calculated: list[CalculatedMeasurement],
    summaries: list[ReplicateSummary],
    out_dir: str | Path,
    expected_roi_per_replicate: int = 3,
) -> Path:
    out = Path(out_dir) / "qc_report.csv"
    rows: list[dict[str, object]] = []
    for row in calculated:
        if row.qc_warning:
            group, repeat = _split_group_repeat(row.group)
            rows.append(
                {
                    "Level": "ROI",
                    "Group": group,
                    "Biological_repeat": repeat,
                    "ROI_label": row.roi_label,
                    "QC_status": row.calculation_status,
                    "Message": row.qc_warning,
                }
            )
    for row in summaries:
        if row.replicate_count < 3:
            rows.append(
                {
                    "Level": "Group",
                    "Group": row.group,
                    "Biological_repeat": "",
                    "ROI_label": "",
                    "QC_status": "WARNING",
                    "Message": f"该组只有 {row.replicate_count} 个生物学重复，少于3个，SD不可靠。",
                }
            )
    for row in summarize_repeats(calculated):
        if row.valid_roi_count < expected_roi_per_replicate:
            rows.append(
                {
                    "Level": "Repeat",
                    "Group": row.group,
                    "Biological_repeat": row.replicate,
                    "ROI_label": "",
                    "QC_status": "WARNING",
                    "Message": f"该重复只有 {row.valid_roi_count} 个有效ROI，少于预设 {expected_roi_per_replicate} 个。",
                }
            )
    if not rows:
        rows.append({"Level": "Run", "Group": "", "Biological_repeat": "", "ROI_label": "", "QC_status": "OK", "Message": "未发现自动QC警告。"})
    write_csv(out, list(rows[0].keys()), rows)
    return out


def write_prism_files(calculated: list[CalculatedMeasurement], out_dir: str | Path, summaries: list[ReplicateSummary] | None = None) -> Path:
    prism = Path(out_dir)
    prism.mkdir(parents=True, exist_ok=True)
    repeat_rows = summarize_repeats(calculated)
    summaries = summaries if summaries is not None else summarize_replicates(calculated)
    all_rows = [
        {
            "Original_group_label": row.group,
            "Group": _split_group_repeat(row.group)[0],
            "Biological_repeat": _split_group_repeat(row.group)[1],
            "ROI_label": row.roi_label,
            "Red_positive_intensity_sum": _fmt(row.red_positive_intensity_sum),
            "Red_corrected_intensity": _fmt(row.red_corrected_intensity),
            "Green_positive_intensity_sum": _fmt(row.green_positive_intensity_sum),
            "Green_corrected_intensity": _fmt(row.green_corrected_intensity),
            "Red_Green_ratio": _fmt(row.red_green_ratio),
            "Dead_percent": _fmt(row.dead_percent),
            "Live_percent": _fmt(row.live_percent),
            "PI_threshold": row.pi_threshold,
            "Calcein_threshold": row.calcein_threshold,
            "QC_warning": row.qc_warning,
        }
        for row in calculated
    ]
    write_csv(prism / "Prism_all_results.csv", list(all_rows[0].keys()) if all_rows else ["Group"], all_rows)
    write_prism_csv_with_mean_appendix(
        prism / "Prism_RedGreen_ratio_column.csv",
        ["Group", "Biological_repeat", "Red_Green_ratio"],
        [{"Group": row.group, "Biological_repeat": row.replicate, "Red_Green_ratio": _fmt(row.red_green_ratio)} for row in repeat_rows],
        ["Group", "Red_Green_ratio_mean", "Red_Green_ratio_SD", "Biological_repeats"],
        [
            {
                "Group": row.group,
                "Red_Green_ratio_mean": _fmt(row.red_green_ratio_mean),
                "Red_Green_ratio_SD": _fmt(row.red_green_ratio_sd),
                "Biological_repeats": row.replicate_count,
            }
            for row in summaries
        ],
    )
    write_prism_csv_with_mean_appendix(
        prism / "Prism_DeadLive_percent_grouped.csv",
        ["Group", "Biological_repeat", "Dead_percent", "Live_percent"],
        [
            {
                "Group": row.group,
                "Biological_repeat": row.replicate,
                "Dead_percent": _fmt(row.dead_percent),
                "Live_percent": _fmt(row.live_percent),
            }
            for row in repeat_rows
        ],
        ["Group", "Dead_percent_mean", "Dead_percent_SD", "Live_percent_mean", "Live_percent_SD", "Biological_repeats"],
        [
            {
                "Group": row.group,
                "Dead_percent_mean": _fmt(row.dead_percent_mean),
                "Dead_percent_SD": _fmt(row.dead_percent_sd),
                "Live_percent_mean": _fmt(row.live_percent_mean),
                "Live_percent_SD": _fmt(row.live_percent_sd),
                "Biological_repeats": row.replicate_count,
            }
            for row in summaries
        ],
    )
    write_repeat_level_csv(repeat_rows, prism, filename="Prism_repeat_level_results.csv")
    write_group_summary_csv(summaries, prism, filename="Prism_group_summary.csv")
    (prism / "Prism_import_instructions.txt").write_text(
        "GraphPad Prism 导入说明\n\n"
        "1. Prism_RedGreen_ratio_column.csv：上半部分为每组3个生物学重复均值，下方“ 三组重复平均值 ”为组mean/SD附表。\n"
        "2. Prism_DeadLive_percent_grouped.csv：上半部分为每组3个生物学重复Dead/Live%，下方为组mean/SD附表。\n"
        "3. Prism_all_results.csv 保留ROI级别审计信息，不建议直接作为生物学重复作图。\n"
        "4. 趋势检查只做提示，不会修改原始数据、阈值或计算结果。\n",
        encoding="utf-8",
    )
    return prism


def _label_lines(label: str) -> list[str]:
    cleaned = label.replace("(", " ").replace(")", " ").replace("+", " + ")
    parts = [part for part in cleaned.split() if part]
    return parts or [label]


def write_svg_figures(summaries: list[ReplicateSummary], out_dir: str | Path) -> Path:
    figures = Path(out_dir)
    figures.mkdir(parents=True, exist_ok=True)
    _write_ratio_svg(summaries, figures / "RedGreen_ratio.svg")
    _write_dead_live_svg(summaries, figures / "DeadLive_percent.svg")
    return figures


def _write_ratio_svg(rows: list[ReplicateSummary], path: Path) -> None:
    width, height = 1200, 760
    left, right, top, bottom = 105, 45, 92, 150
    plot_w, plot_h = width - left - right, height - top - bottom
    max_value = max(
        (
            row.red_green_ratio_mean + (0 if math.isnan(row.red_green_ratio_sd) else row.red_green_ratio_sd)
            for row in rows
            if not math.isnan(row.red_green_ratio_mean)
        ),
        default=1.0,
    )
    max_y = max(0.1, math.ceil(max_value * 10 + 1) / 10)
    bar_w = min(92, plot_w / max(1, len(rows)) * 0.48)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        f'<text x="{width/2}" y="42" text-anchor="middle" font-family="Arial" font-size="31" font-weight="700">PI red / Calcein-AM green ratio</text>',
        f'<text x="{width/2}" y="72" text-anchor="middle" font-family="Arial" font-size="17" fill="#444">Repeat means with group SD</text>',
        f'<line x1="{left}" y1="{top+plot_h}" x2="{left+plot_w}" y2="{top+plot_h}" stroke="#222" stroke-width="2"/>',
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top+plot_h}" stroke="#222" stroke-width="2"/>',
        f'<text x="30" y="{top+plot_h/2}" transform="rotate(-90,30,{top+plot_h/2})" text-anchor="middle" font-family="Arial" font-size="21">Red / Green ratio</text>',
    ]
    for tick in range(6):
        value = max_y / 5 * tick
        y = top + plot_h - (value / max_y) * plot_h
        parts.append(f'<line x1="{left-8}" y1="{y}" x2="{left+plot_w}" y2="{y}" stroke="{"#222" if tick == 0 else "#e5e7eb"}" stroke-width="{"2" if tick == 0 else "1"}"/>')
        parts.append(f'<text x="{left-16}" y="{y+6}" text-anchor="end" font-family="Arial" font-size="16" fill="#333">{value:.2f}</text>')
    for i, row in enumerate(rows):
        cx = left + plot_w / len(rows) * i + plot_w / len(rows) / 2
        value = 0 if math.isnan(row.red_green_ratio_mean) else row.red_green_ratio_mean
        sd = 0 if math.isnan(row.red_green_ratio_sd) else row.red_green_ratio_sd
        bar_h = value / max_y * plot_h
        x = cx - bar_w / 2
        y = top + plot_h - bar_h
        parts.append(f'<rect x="{x}" y="{y}" width="{bar_w}" height="{bar_h}" fill="#8B1E1E" opacity="0.9"/>')
        if sd:
            y_low = top + plot_h - max(0, value - sd) / max_y * plot_h
            y_high = top + plot_h - min(max_y, value + sd) / max_y * plot_h
            parts.append(f'<line x1="{cx}" y1="{y_low}" x2="{cx}" y2="{y_high}" stroke="#111" stroke-width="2"/>')
            parts.append(f'<line x1="{cx-15}" y1="{y_low}" x2="{cx+15}" y2="{y_low}" stroke="#111" stroke-width="2"/>')
            parts.append(f'<line x1="{cx-15}" y1="{y_high}" x2="{cx+15}" y2="{y_high}" stroke="#111" stroke-width="2"/>')
        parts.append(f'<text x="{cx}" y="{y-10}" text-anchor="middle" font-family="Arial" font-size="17" fill="#8B1E1E">{value:.3f}</text>')
        for line_index, label in enumerate(_label_lines(row.group)):
            parts.append(f'<text x="{cx}" y="{top+plot_h+34+line_index*19}" text-anchor="middle" font-family="Arial" font-size="15" fill="#222">{escape(label)}</text>')
    parts.append("</svg>")
    path.write_text("\n".join(parts), encoding="utf-8")


def _write_dead_live_svg(rows: list[ReplicateSummary], path: Path) -> None:
    width, height = 1200, 760
    left, right, top, bottom = 105, 45, 92, 150
    plot_w, plot_h = width - left - right, height - top - bottom
    bar_w = min(96, plot_w / max(1, len(rows)) * 0.48)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        f'<text x="{width/2}" y="42" text-anchor="middle" font-family="Arial" font-size="31" font-weight="700">Live/dead corrected intensity share</text>',
        f'<text x="{width/2}" y="72" text-anchor="middle" font-family="Arial" font-size="17" fill="#444">PI red = dead, Calcein-AM green = live</text>',
        f'<line x1="{left}" y1="{top+plot_h}" x2="{left+plot_w}" y2="{top+plot_h}" stroke="#222" stroke-width="2"/>',
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top+plot_h}" stroke="#222" stroke-width="2"/>',
        f'<text x="30" y="{top+plot_h/2}" transform="rotate(-90,30,{top+plot_h/2})" text-anchor="middle" font-family="Arial" font-size="21">Corrected intensity share (%)</text>',
    ]
    for value in range(0, 101, 20):
        y = top + plot_h - (value / 100) * plot_h
        parts.append(f'<line x1="{left-8}" y1="{y}" x2="{left+plot_w}" y2="{y}" stroke="{"#222" if value == 0 else "#e5e7eb"}" stroke-width="{"2" if value == 0 else "1"}"/>')
        parts.append(f'<text x="{left-16}" y="{y+6}" text-anchor="end" font-family="Arial" font-size="16" fill="#333">{value}</text>')
    for i, row in enumerate(rows):
        cx = left + plot_w / len(rows) * i + plot_w / len(rows) / 2
        x = cx - bar_w / 2
        base = top + plot_h
        live_percent = 0 if math.isnan(row.live_percent_mean) else row.live_percent_mean
        dead_percent = 0 if math.isnan(row.dead_percent_mean) else row.dead_percent_mean
        live_h = live_percent / 100 * plot_h
        dead_h = dead_percent / 100 * plot_h
        parts.append(f'<rect x="{x}" y="{base-live_h}" width="{bar_w}" height="{live_h}" fill="#16A34A"/>')
        parts.append(f'<rect x="{x}" y="{base-live_h-dead_h}" width="{bar_w}" height="{dead_h}" fill="#EF4444"/>')
        parts.append(f'<text x="{cx}" y="{base-live_h-dead_h/2+6}" text-anchor="middle" font-family="Arial" font-size="15" font-weight="700" fill="white">{dead_percent:.1f}%</text>')
        for line_index, label in enumerate(_label_lines(row.group)):
            parts.append(f'<text x="{cx}" y="{top+plot_h+34+line_index*19}" text-anchor="middle" font-family="Arial" font-size="15" fill="#222">{escape(label)}</text>')
    parts.append(f'<rect x="{left+20}" y="{height-58}" width="18" height="18" fill="#EF4444"/><text x="{left+47}" y="{height-43}" font-family="Arial" font-size="17">Dead %</text>')
    parts.append(f'<rect x="{left+140}" y="{height-58}" width="18" height="18" fill="#16A34A"/><text x="{left+167}" y="{height-43}" font-family="Arial" font-size="17">Live %</text>')
    parts.append("</svg>")
    path.write_text("\n".join(parts), encoding="utf-8")


def write_summary(summaries: list[ReplicateSummary], trend_check: TrendCheck, out_dir: str | Path) -> Path:
    path = Path(out_dir) / "summary.txt"
    lines = [
        (
            f"{row.group}\tn={row.replicate_count}"
            f"\tRed/Green={row.red_green_ratio_mean:.4f}±{row.red_green_ratio_sd:.4f}"
            f"\tDead%={row.dead_percent_mean:.2f}±{row.dead_percent_sd:.2f}"
            f"\tLive%={row.live_percent_mean:.2f}±{row.live_percent_sd:.2f}"
        )
        for row in summaries
    ]
    lines.append("")
    lines.append(f"趋势检查: {trend_check.message}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_html_report(
    raw: list[RawMeasurement],
    calculated: list[CalculatedMeasurement],
    repeats: list[RepeatSummary],
    summaries: list[ReplicateSummary],
    trend_check: TrendCheck,
    out_dir: str | Path,
) -> Path:
    out = Path(out_dir)
    summary_rows = []
    for row in summaries:
        summary_rows.append(
            "<tr>"
            f"<td>{html.escape(row.group)}</td>"
            f"<td>{row.replicate_count}</td>"
            f"<td>{_fmt(row.red_green_ratio_mean, 4)}</td>"
            f"<td>{_fmt(row.red_green_ratio_sd, 4)}</td>"
            f"<td>{_fmt(row.dead_percent_mean, 2)}</td>"
            f"<td>{_fmt(row.dead_percent_sd, 2)}</td>"
            f"<td>{_fmt(row.live_percent_mean, 2)}</td>"
            f"<td>{_fmt(row.live_percent_sd, 2)}</td>"
            "</tr>"
        )
    repeat_rows = []
    for row in repeats:
        repeat_rows.append(
            "<tr>"
            f"<td>{html.escape(row.group)}</td>"
            f"<td>{html.escape(row.replicate)}</td>"
            f"<td>{row.valid_roi_count}</td>"
            f"<td>{_fmt(row.red_green_ratio, 4)}</td>"
            f"<td>{_fmt(row.dead_percent, 2)}</td>"
            f"<td>{_fmt(row.live_percent, 2)}</td>"
            "</tr>"
        )
    roi_rows = []
    for calc in calculated:
        group, repeat = _split_group_repeat(calc.group)
        roi_rows.append(
            "<tr>"
            f"<td>{html.escape(group)}</td>"
            f"<td>{html.escape(repeat)}</td>"
            f"<td>{html.escape(calc.roi_label)}</td>"
            f"<td>{_fmt(calc.red_corrected_intensity)}</td>"
            f"<td>{_fmt(calc.green_corrected_intensity)}</td>"
            f"<td>{_fmt(calc.red_green_ratio, 4)}</td>"
            f"<td>{_fmt(calc.dead_percent, 2)}</td>"
            f"<td>{_fmt(calc.live_percent, 2)}</td>"
            f"<td>{html.escape(calc.qc_warning)}</td>"
            "</tr>"
        )
    doc = f"""<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<title>ImageJ fluorescence report</title>
<style>
body{{font-family:Arial,"PingFang SC",sans-serif;margin:28px;color:#111;line-height:1.45}}
table{{border-collapse:collapse;width:100%;font-size:14px}}
th,td{{border:1px solid #d1d5db;padding:7px 9px;text-align:right}}
th:first-child,td:first-child{{text-align:left}}
th{{background:#f3f4f6}}
.figs img{{max-width:48%;border:1px solid #ddd;margin:8px}}
.trend{{padding:10px 12px;border-radius:6px;margin:12px 0;background:#f3f4f6}}
.trend.ok{{background:#ecfdf5;color:#065f46}}
.trend.warning{{background:#fffbeb;color:#92400e}}
code{{background:#f5f5f5;padding:2px 4px}}
</style>
<h1>ImageJ fluorescence quantification report</h1>
<p>生成时间：{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</p>
<p><code>Dead% = red corrected intensity / (red corrected intensity + green corrected intensity)</code>；同一重复内先平均ROI，再以重复均值计算组mean/SD。</p>
<div class="trend {'ok' if trend_check.passed else 'warning'}">{html.escape(trend_check.message)}</div>
<h2>Group summary</h2>
<table><thead><tr><th>Group</th><th>重复数</th><th>Red/Green mean</th><th>Red/Green SD</th><th>Dead % mean</th><th>Dead % SD</th><th>Live % mean</th><th>Live % SD</th></tr></thead><tbody>
{''.join(summary_rows)}
</tbody></table>
<h2>Repeat-level means</h2>
<table><thead><tr><th>Group</th><th>重复编号</th><th>有效ROI数</th><th>Red/Green</th><th>Dead %</th><th>Live %</th></tr></thead><tbody>
{''.join(repeat_rows)}
</tbody></table>
<h2>ROI-level audit</h2>
<table><thead><tr><th>Group</th><th>重复编号</th><th>ROI</th><th>Red corrected</th><th>Green corrected</th><th>Red/Green</th><th>Dead %</th><th>Live %</th><th>QC</th></tr></thead><tbody>
{''.join(roi_rows)}
</tbody></table>
<div class="figs"><h2>Figures</h2><img src="figures/RedGreen_ratio.svg"><img src="figures/DeadLive_percent.svg"></div>
</html>
"""
    path = out / "report.html"
    path.write_text(doc, encoding="utf-8")
    return path


def build_all_reports(
    raw: list[RawMeasurement],
    out_dir: str | Path,
    expected_trend: str = "none",
    analysis_settings: dict[str, object] | None = None,
) -> list[CalculatedMeasurement]:
    out = Path(out_dir)
    calculated = calculate_all(raw)
    repeats = summarize_repeats(calculated)
    summaries = summarize_replicates(calculated)
    trend_check = check_expected_trend(
        summaries,
        expected_trend,
        min_value=(analysis_settings or {}).get("trend_min_value"),  # type: ignore[arg-type]
        max_value=(analysis_settings or {}).get("trend_max_value"),  # type: ignore[arg-type]
    )
    write_calculated_csv(calculated, out)
    write_roi_level_csv(calculated, out)
    write_repeat_level_csv(repeats, out)
    write_group_summary_csv(summaries, out)
    write_replicate_summary_csv(summaries, out)
    write_threshold_log_csv(calculated, out)
    expected_roi_per_replicate = int((analysis_settings or {}).get("roi_per_replicate", 3))
    write_qc_report_csv(calculated, summaries, out, expected_roi_per_replicate=expected_roi_per_replicate)
    write_prism_files(calculated, out / "prism", summaries=summaries)
    write_svg_figures(summaries, out / "figures")
    write_formula_workbook(raw, out / "AUR_ImageJ_fluorescence_formula_results.xlsx")
    write_summary(summaries, trend_check, out)
    write_html_report(raw, calculated, repeats, summaries, trend_check, out)
    settings = dict(analysis_settings or {})
    settings["trend_notice"] = NO_MUTATION_NOTICE
    settings["expected_trend"] = expected_trend
    (out / "analysis_settings.json").write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    return calculated


def _safe_name(name: str) -> str:
    safe = name.replace(" ", "_")
    safe = safe.replace("+", "_")
    safe = safe.replace("(", "")
    safe = safe.replace(")", "")
    for char in ["/", "\\", ",", "，"]:
        safe = safe.replace(char, "_")
    return safe


def write_formula_workbook(raw_measurements: list[RawMeasurement], xlsx_path: str | Path) -> Path:
    xlsx = Path(xlsx_path)
    sheets = {
        "Protocol": _protocol_rows(),
        "ImageJ Raw": _raw_rows(raw_measurements),
        "Calculations": _calculation_rows(raw_measurements),
        "Repeat Means": _repeat_formula_rows(raw_measurements),
        "Group Summary": _group_formula_rows(raw_measurements),
        "Prism Data": _prism_rows(raw_measurements),
    }
    with zipfile.ZipFile(xlsx, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _content_types(len(sheets)))
        zf.writestr("_rels/.rels", _root_rels())
        zf.writestr("docProps/core.xml", _core_props())
        zf.writestr("docProps/app.xml", _app_props())
        zf.writestr("xl/workbook.xml", _workbook_xml(list(sheets)))
        zf.writestr("xl/_rels/workbook.xml.rels", _workbook_rels(len(sheets)))
        zf.writestr("xl/styles.xml", _styles_xml())
        for index, rows in enumerate(sheets.values(), start=1):
            zf.writestr(f"xl/worksheets/sheet{index}.xml", _sheet_xml(rows))
    return xlsx


def _protocol_rows() -> list[list[object]]:
    return [
        ["AUR fluorescence quantification with Fiji/ImageJ"],
        [],
        ["Channel logic", "PI red = dead; Calcein-AM green = live"],
        ["Dead % formula", "Red corrected intensity / (Red corrected intensity + Green corrected intensity)"],
        ["Repeat rule", "Average ROI values within each biological repeat first; group mean/SD use repeat-level values."],
        ["Trend policy", NO_MUTATION_NOTICE],
    ]


def _raw_rows(raws: list[RawMeasurement]) -> list[list[object]]:
    rows: list[list[object]] = [[
        "Group",
        "Red image",
        "Green image",
        "Merge image",
        "PI threshold",
        "Calcein threshold",
        "Red positive pixels",
        "Green positive pixels",
        "Red mean positive intensity",
        "Green mean positive intensity",
        "ROI label",
        "ROI center x",
        "ROI center y",
        "ROI radius",
        "ROI area",
        "Red raw integrated density",
        "Red positive intensity sum",
        "Red background mean",
        "Red corrected intensity raw",
        "Red saturated pixel percent",
        "Green raw integrated density",
        "Green positive intensity sum",
        "Green background mean",
        "Green corrected intensity raw",
        "Green saturated pixel percent",
        "Saturated pixel percent",
    ]]
    for raw in raws:
        rows.append([
            raw.group,
            raw.red_image,
            raw.green_image,
            raw.merge_image,
            raw.red_threshold_otsu,
            raw.green_threshold_otsu,
            raw.red_positive_pixels,
            raw.green_positive_pixels,
            raw.red_mean_positive_intensity,
            raw.green_mean_positive_intensity,
            raw.roi_label,
            raw.roi_x,
            raw.roi_y,
            raw.roi_radius,
            raw.roi_area,
            raw.red_raw_integrated_density,
            raw.red_positive_intensity_sum,
            raw.red_background_mean,
            raw.red_corrected_intensity_raw,
            raw.red_saturated_pixel_percent,
            raw.green_raw_integrated_density,
            raw.green_positive_intensity_sum,
            raw.green_background_mean,
            raw.green_corrected_intensity_raw,
            raw.green_saturated_pixel_percent,
            raw.saturated_pixel_percent,
        ])
    return rows


def _calculation_rows(raws: list[RawMeasurement]) -> list[list[object]]:
    rows: list[list[object]] = [[
        "Group",
        "Base group",
        "Biological repeat",
        "ROI label",
        "Red corrected intensity",
        "Green corrected intensity",
        "Total corrected intensity",
        "Red/Green ratio",
        "Dead %",
        "Live %",
        "Status",
    ]]
    for row_index, raw in enumerate(raws, start=2):
        base_group, repeat = _split_group_repeat(raw.group)
        rows.append([
            raw.group,
            base_group,
            repeat,
            raw.roi_label,
            {"formula": f"MAX(0,'ImageJ Raw'!S{row_index})"},
            {"formula": f"MAX(0,'ImageJ Raw'!X{row_index})"},
            {"formula": f"E{row_index}+F{row_index}"},
            {"formula": f'IF(F{row_index}=0,"",E{row_index}/F{row_index})'},
            {"formula": f'IF(G{row_index}=0,"",E{row_index}/G{row_index}*100)'},
            {"formula": f'IF(G{row_index}=0,"",F{row_index}/G{row_index}*100)'},
            {"formula": f'IF(G{row_index}=0,"No signal","OK")'},
        ])
    return rows


def _formula_refs(sheet: str, column: str, row_indices: list[int]) -> str:
    return ",".join(f"'{sheet}'!{column}{row_index}" for row_index in row_indices)


def _repeat_formula_rows(raws: list[RawMeasurement]) -> list[list[object]]:
    rows: list[list[object]] = [[
        "Group",
        "Biological repeat",
        "ROI count",
        "Red corrected mean",
        "Green corrected mean",
        "Red/Green ratio",
        "Dead %",
        "Live %",
    ]]
    grouped_rows: dict[tuple[str, str], list[int]] = {}
    for calculation_row_index, raw in enumerate(raws, start=2):
        grouped_rows.setdefault(_split_group_repeat(raw.group), []).append(calculation_row_index)

    for group, repeat in grouped_rows:
        calc_rows = grouped_rows[(group, repeat)]
        red_refs = _formula_refs("Calculations", "E", calc_rows)
        green_refs = _formula_refs("Calculations", "F", calc_rows)
        rows.append([
            group,
            repeat,
            {"formula": f"COUNT({red_refs})"},
            {"formula": f"AVERAGE({red_refs})"},
            {"formula": f"AVERAGE({green_refs})"},
            {"formula": f'IF(E{len(rows)+1}=0,"",D{len(rows)+1}/E{len(rows)+1})'},
            {"formula": f'IF(D{len(rows)+1}+E{len(rows)+1}=0,"",D{len(rows)+1}/(D{len(rows)+1}+E{len(rows)+1})*100)'},
            {"formula": f'IF(D{len(rows)+1}+E{len(rows)+1}=0,"",E{len(rows)+1}/(D{len(rows)+1}+E{len(rows)+1})*100)'},
        ])
    return rows


def _group_formula_rows(raws: list[RawMeasurement]) -> list[list[object]]:
    repeat_rows = _repeat_formula_rows(raws)
    rows: list[list[object]] = [[
        "Group",
        "Biological repeats",
        "Red_Green_ratio_mean",
        "Red_Green_ratio_SD",
        "Dead_percent_mean",
        "Dead_percent_SD",
        "Live_percent_mean",
        "Live_percent_SD",
    ]]
    grouped_repeat_rows: dict[str, list[int]] = {}
    for sheet_row_index, repeat_row in enumerate(repeat_rows[1:], start=2):
        grouped_repeat_rows.setdefault(str(repeat_row[0]), []).append(sheet_row_index)

    for group, row_indices in grouped_repeat_rows.items():
        ratio_refs = _formula_refs("Repeat Means", "F", row_indices)
        dead_refs = _formula_refs("Repeat Means", "G", row_indices)
        live_refs = _formula_refs("Repeat Means", "H", row_indices)
        rows.append([
            group,
            {"formula": f"COUNT({ratio_refs})"},
            {"formula": f"AVERAGE({ratio_refs})"},
            {"formula": f"STDEV.S({ratio_refs})"},
            {"formula": f"AVERAGE({dead_refs})"},
            {"formula": f"STDEV.S({dead_refs})"},
            {"formula": f"AVERAGE({live_refs})"},
            {"formula": f"STDEV.S({live_refs})"},
        ])
    return rows


def _prism_rows(raws: list[RawMeasurement]) -> list[list[object]]:
    repeat_rows = _repeat_formula_rows(raws)
    group_rows = _group_formula_rows(raws)
    rows: list[list[object]] = [["Group", "Biological repeat", "Red_Green_ratio", "Dead_percent", "Live_percent", "", "三组重复平均值", "", "", "", ""]]
    for repeat_sheet_row, repeat_row in enumerate(repeat_rows[1:], start=2):
        rows.append([
            repeat_row[0],
            repeat_row[1],
            {"formula": f"'Repeat Means'!F{repeat_sheet_row}"},
            {"formula": f"'Repeat Means'!G{repeat_sheet_row}"},
            {"formula": f"'Repeat Means'!H{repeat_sheet_row}"},
            "",
            "",
            "",
            "",
            "",
            "",
        ])
    rows.append([])
    rows.append(["Group", "Red_Green_ratio_mean", "Red_Green_ratio_SD", "Dead_percent_mean", "Dead_percent_SD", "Live_percent_mean", "Live_percent_SD"])
    for group_sheet_row, group_row in enumerate(group_rows[1:], start=2):
        rows.append([
            group_row[0],
            {"formula": f"'Group Summary'!C{group_sheet_row}"},
            {"formula": f"'Group Summary'!D{group_sheet_row}"},
            {"formula": f"'Group Summary'!E{group_sheet_row}"},
            {"formula": f"'Group Summary'!F{group_sheet_row}"},
            {"formula": f"'Group Summary'!G{group_sheet_row}"},
            {"formula": f"'Group Summary'!H{group_sheet_row}"},
        ])
    return rows


def _col_name(index: int) -> str:
    result = ""
    while index:
        index, rem = divmod(index - 1, 26)
        result = chr(65 + rem) + result
    return result


def _sheet_xml(rows: list[list[object]]) -> str:
    xml_rows = []
    for r_index, row in enumerate(rows, start=1):
        cells = []
        for c_index, value in enumerate(row, start=1):
            ref = f"{_col_name(c_index)}{r_index}"
            if value == "":
                cells.append(f'<c r="{ref}"/>')
            elif isinstance(value, dict) and "formula" in value:
                cells.append(f'<c r="{ref}"><f>{escape(str(value["formula"]))}</f></c>')
            elif isinstance(value, (int, float)):
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
            else:
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>')
        xml_rows.append(f'<row r="{r_index}">{"".join(cells)}</row>')
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetViews><sheetView workbookViewId="0" showGridLines="0"/></sheetViews>'
        f'<sheetData>{"".join(xml_rows)}</sheetData>'
        "</worksheet>"
    )


def _content_types(sheet_count: int) -> str:
    sheets = "".join(
        f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for i in range(1, sheet_count + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
        f"{sheets}</Types>"
    )


def _root_rels() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
        "</Relationships>"
    )


def _workbook_xml(sheet_names: list[str]) -> str:
    sheets = "".join(
        f'<sheet name="{escape(name)}" sheetId="{index}" r:id="rId{index}"/>'
        for index, name in enumerate(sheet_names, start=1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        "<workbookPr/>"
        "<bookViews><workbookView/></bookViews>"
        f"<sheets>{sheets}</sheets>"
        '<calcPr calcMode="auto" fullCalcOnLoad="1" forceFullCalc="1"/>'
        "</workbook>"
    )


def _workbook_rels(sheet_count: int) -> str:
    rels = "".join(
        f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
        for i in range(1, sheet_count + 1)
    )
    rels += f'<Relationship Id="rId{sheet_count + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}</Relationships>'
    )


def _styles_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="1"><font><sz val="11"/><name val="Arial"/></font></fonts>'
        '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
        '<borders count="1"><border/></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        "</styleSheet>"
    )


def _core_props() -> str:
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:dcmitype="http://purl.org/dc/dcmitype/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        "<dc:creator>ImageJ Automation</dc:creator>"
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>'
        "</cp:coreProperties>"
    )


def _app_props() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
        'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
        "<Application>ImageJ Automation</Application>"
        "</Properties>"
    )
