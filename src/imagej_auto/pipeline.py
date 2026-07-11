from __future__ import annotations

import argparse
import json
import math
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Callable

from .grouping import build_triplets, parse_group_names, parse_order, parse_replicates_per_group
from .imagej_macro import build_macro
from .imagej_runner import run_imagej_macro
from .models import CalculatedMeasurement, PipelineOptions, RawMeasurement
from .ppt_extractor import extract_ppt_images
from .report_builder import (
    build_all_reports,
    check_expected_trend,
    describe_expected_trend,
    parse_raw_measurements,
    summarize_repeats,
    summarize_replicates,
)


LogFn = Callable[[str], None]


def _log(log: LogFn | None, message: str) -> None:
    if log:
        log(message)


def _split_replicate_group(group: str) -> tuple[str, str]:
    match = re.fullmatch(r"(.+)\s+(R\d+)", group)
    if not match:
        return group, ""
    return match.group(1), match.group(2)


def _round_or_none(value: float, digits: int) -> float | None:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return round(value, digits)


def _has_mixed_pbmc_groups(groups: list[str]) -> bool:
    has_pbmc = any("pbmc" in group.lower() for group in groups)
    has_no_pbmc = any("pbmc" not in group.lower() for group in groups)
    return has_pbmc and has_no_pbmc


SUPPORTED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}


def copy_image_inputs(image_paths: list[str | Path], extracted_dir: str | Path) -> list[Path]:
    if len(image_paths) < 3:
        raise ValueError("直接上传图片至少需要3张，按红/绿/合并三张组成一组。")
    destination = Path(extracted_dir)
    destination.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    for index, source_value in enumerate(image_paths, start=1):
        source = Path(source_value).expanduser()
        suffix = source.suffix.lower()
        if suffix not in SUPPORTED_IMAGE_EXTENSIONS:
            raise ValueError(f"不支持的图片格式: {source.name}")
        target = destination / f"image{index:04d}{suffix}"
        shutil.copy2(source, target)
        copied.append(target)
    return copied


def validate_threshold_results(raw: list[RawMeasurement], options: PipelineOptions) -> dict[str, object]:
    if not raw:
        raise ValueError("没有可验证的阈值结果。")
    red_values = sorted({row.red_threshold_otsu for row in raw})
    green_values = sorted({row.green_threshold_otsu for row in raw})
    if any(value < 0 or value > 254 for value in red_values + green_values):
        raise RuntimeError("ImageJ 导出的阈值超出0-254范围。")

    validation: dict[str, object] = {
        "passed": True,
        "scope": options.threshold_scope,
        "red_values": red_values,
        "green_values": green_values,
        "message": "阈值已由 ImageJ 写入逐ROI结果。",
    }
    if options.threshold_scope == "fixed":
        expected_red = max(options.red_fixed_threshold, options.min_threshold) if options.use_min_threshold else options.red_fixed_threshold
        expected_green = max(options.green_fixed_threshold, options.min_threshold) if options.use_min_threshold else options.green_fixed_threshold
        passed = red_values == [expected_red] and green_values == [expected_green]
        validation.update(
            {
                "passed": passed,
                "expected_red": expected_red,
                "expected_green": expected_green,
                "message": (
                    f"固定阈值验证通过：Red={expected_red}, Green={expected_green}。"
                    if passed
                    else f"固定阈值未生效：期望 Red={expected_red}, Green={expected_green}；实际 Red={red_values}, Green={green_values}。"
                ),
            }
        )
        if not passed:
            raise RuntimeError(str(validation["message"]))
    return validation


def build_result_payload(
    run_dir: str | Path,
    raw_csv: str | Path,
    calculated: list[CalculatedMeasurement],
    replicates_per_group: int,
    expected_trend: str,
    roi_per_replicate: int = 3,
    trend_min_value: float | None = None,
    trend_max_value: float | None = None,
    threshold_validation: dict[str, object] | None = None,
) -> dict[str, object]:
    run_dir_path = Path(run_dir)
    raw_csv_path = Path(raw_csv)
    repeat_rows = summarize_repeats(calculated)
    summaries = summarize_replicates(calculated)
    trend_check = check_expected_trend(
        summaries,
        expected_trend,
        min_value=trend_min_value,
        max_value=trend_max_value,
    )
    return {
        "run_dir": str(run_dir_path),
        "groups": len(summaries),
        "replicates_per_group": replicates_per_group,
        "replicates_per_group_label": f"每组 {replicates_per_group} 次重复",
        "roi_per_replicate": roi_per_replicate,
        "roi_per_replicate_label": f"每个重复 {roi_per_replicate} 个 ROI",
        "summary": [
            {
                "group": row.group,
                "replicate_count": row.replicate_count,
                "replicate_count_label": f"{row.replicate_count} 次重复",
                "red_green_ratio_mean": _round_or_none(row.red_green_ratio_mean, 4),
                "red_green_ratio_sd": _round_or_none(row.red_green_ratio_sd, 4),
                "dead_percent_mean": _round_or_none(row.dead_percent_mean, 2),
                "dead_percent_sd": _round_or_none(row.dead_percent_sd, 2),
                "live_percent_mean": _round_or_none(row.live_percent_mean, 2),
                "live_percent_sd": _round_or_none(row.live_percent_sd, 2),
            }
            for row in summaries
        ],
        "repeat_level": [
            {
                "group": row.group,
                "replicate": row.replicate,
                "valid_roi_count": row.valid_roi_count,
                "red_green_ratio": _round_or_none(row.red_green_ratio, 4),
                "dead_percent": _round_or_none(row.dead_percent, 2),
                "live_percent": _round_or_none(row.live_percent, 2),
                "has_qc_warning": row.has_qc_warning,
            }
            for row in repeat_rows
        ],
        "measurements": [
            {
                "group": _split_replicate_group(row.group)[0],
                "replicate": _split_replicate_group(row.group)[1],
                "display_group": row.group,
                "red_green_ratio": _round_or_none(row.red_green_ratio, 4),
                "dead_percent": _round_or_none(row.dead_percent, 2),
                "live_percent": _round_or_none(row.live_percent, 2),
                "roi_label": row.roi_label,
                "roi_x": row.roi_x,
                "roi_y": row.roi_y,
                "roi_radius": row.roi_radius,
                "red_corrected_intensity": _round_or_none(row.red_corrected_intensity, 6),
                "green_corrected_intensity": _round_or_none(row.green_corrected_intensity, 6),
                "qc_warning": row.qc_warning,
            }
            for row in calculated
        ],
        "trend": {
            "expected_trend": trend_check.expected_trend,
            "selected_label": trend_check.selected_label,
            "metric_label": trend_check.metric_label,
            "direction_label": trend_check.direction_label,
            "passed": trend_check.passed,
            "min_value": trend_min_value,
            "max_value": trend_max_value,
            "message": trend_check.message,
            "points": [{"group": point.group, "value": _round_or_none(point.value, 4)} for point in trend_check.points],
        },
        "threshold_validation": threshold_validation or {},
        "files": {
            "excel": str(run_dir_path / "AUR_ImageJ_fluorescence_formula_results.xlsx"),
            "report": str(run_dir_path / "report.html"),
            "raw_csv": str(raw_csv_path),
            "calculated_csv": str(run_dir_path / "calculated_results.csv"),
            "roi_level_csv": str(run_dir_path / "roi_level_results.csv"),
            "repeat_level_csv": str(run_dir_path / "repeat_level_results.csv"),
            "group_summary_csv": str(run_dir_path / "group_summary.csv"),
            "replicate_summary_csv": str(run_dir_path / "replicate_summary.csv"),
            "threshold_log_csv": str(run_dir_path / "threshold_log.csv"),
            "qc_report_csv": str(run_dir_path / "qc_report.csv"),
            "analysis_settings": str(run_dir_path / "analysis_settings.json"),
            "prism_dir": str(run_dir_path / "prism"),
            "figures_dir": str(run_dir_path / "figures"),
            "masks_dir": str(run_dir_path / "imagej_masks"),
        },
    }


def _new_run_dir(output_root: str | Path) -> Path:
    output_root_path = Path(output_root).expanduser()
    output_root_path.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = output_root_path / f"imagej_run_{timestamp}"
    counter = 1
    while run_dir.exists():
        run_dir = output_root_path / f"imagej_run_{timestamp}_{counter}"
        counter += 1
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def _run_prepared_images(
    run_dir: Path,
    extracted_dir: Path,
    images: list[Path],
    options: PipelineOptions,
    input_type: str,
    log: LogFn | None = None,
) -> dict[str, object]:
    if options.threshold_scope not in {"fixed", "per_image_otsu", "control_fixed"}:
        raise ValueError("阈值范围选项无效。")
    if not 0 <= options.red_fixed_threshold <= 254 or not 0 <= options.green_fixed_threshold <= 254:
        raise ValueError("红色和绿色固定阈值必须在0到254之间。")
    if options.trend_min_value is not None and not 0 <= options.trend_min_value <= 100:
        raise ValueError("趋势最小值必须在0到100之间。")
    if options.trend_max_value is not None and not 0 <= options.trend_max_value <= 100:
        raise ValueError("趋势最大值必须在0到100之间。")
    if (
        options.trend_min_value is not None
        and options.trend_max_value is not None
        and options.trend_min_value > options.trend_max_value
    ):
        raise ValueError("趋势最小值不能大于最大值。")
    selected_trend_label = describe_expected_trend(options.expected_trend)

    _log(log, f"用户预期趋势: {selected_trend_label}")
    if len(images) % 3:
        _log(log, f"提示: 图片数量不是 3 的倍数，末尾 {len(images) % 3} 张未参与分组。")

    triplets = build_triplets(
        images,
        group_names=options.group_names,
        order=options.order,
        replicates_per_group=options.replicates_per_group,
    )
    _log(
        log,
        f"检测分组: {len(triplets)} 个实验组，每组 {options.replicates_per_group} 次重复；每个重复圈取 {options.roi_per_replicate} 个圆形 ROI",
    )
    if options.dead_percent_mode == "cell_count":
        raise ValueError("细胞计数模式需要真实细胞分割/计数结果；当前版本不会用强度或像素数据冒充细胞计数。请使用默认“校正强度比例”。")
    pbmc_warning = ""
    if options.dead_percent_mode == "intensity_ratio" and _has_mixed_pbmc_groups([triplet.group for triplet in triplets]):
        pbmc_warning = "检测到PBMC与非PBMC组同时存在；强度比例会受PBMC背景/密度影响，请结合ROI与QC表审阅。"
        _log(log, f"提示: {pbmc_warning}")
    mapping = [
        {
            "group": triplet.group,
            "red": triplet.red.name,
            "green": triplet.green.name,
            "merge": triplet.merge.name,
        }
        for triplet in triplets
    ]
    (run_dir / "group_mapping.json").write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")

    macro_text = build_macro(
        triplets,
        threshold_method=options.threshold_method,
        min_threshold=options.min_threshold,
        repeat_count=options.replicates_per_group,
        roi_per_repeat=options.roi_per_replicate,
        threshold_scope=options.threshold_scope,
        red_fixed_threshold=options.red_fixed_threshold,
        green_fixed_threshold=options.green_fixed_threshold,
        use_min_threshold=options.use_min_threshold,
        background_subtraction=options.background_subtraction,
        dead_percent_mode=options.dead_percent_mode,
    )
    macro_path = run_dir / "ImageJ_fluorescence_quantification.ijm"
    macro_path.write_text(macro_text, encoding="utf-8")
    _log(log, "ImageJ 宏已生成")

    run_imagej_macro(macro_path, extracted_dir, run_dir, fiji_path=options.fiji_path)
    raw_csv = run_dir / "imagej_raw_measurements.csv"
    raw = parse_raw_measurements(raw_csv)
    threshold_validation = validate_threshold_results(raw, options)
    _log(log, str(threshold_validation["message"]))
    calculated = build_all_reports(
        raw,
        run_dir,
        expected_trend=options.expected_trend,
        analysis_settings={
            "threshold_method": options.threshold_method,
            "threshold_scope": options.threshold_scope,
            "min_threshold": options.min_threshold,
            "red_fixed_threshold": options.red_fixed_threshold,
            "green_fixed_threshold": options.green_fixed_threshold,
            "use_min_threshold": options.use_min_threshold,
            "background_subtraction": options.background_subtraction,
            "dead_percent_mode": options.dead_percent_mode,
            "roi_mode": options.roi_mode,
            "replicates_per_group": options.replicates_per_group,
            "roi_per_replicate": options.roi_per_replicate,
            "group_names": options.group_names,
            "order": options.order,
            "pbmc_warning": pbmc_warning,
            "input_type": input_type,
            "trend_min_value": options.trend_min_value,
            "trend_max_value": options.trend_max_value,
            "threshold_validation": threshold_validation,
        },
    )
    result = build_result_payload(
        run_dir,
        raw_csv,
        calculated,
        options.replicates_per_group,
        options.expected_trend,
        roi_per_replicate=options.roi_per_replicate,
        trend_min_value=options.trend_min_value,
        trend_max_value=options.trend_max_value,
        threshold_validation=threshold_validation,
    )
    result["input_type"] = input_type
    _log(log, "Excel、Prism 数据、SVG 图和 HTML 报告已生成")
    _log(log, f"趋势检查: {result['trend']['message']}")  # type: ignore[index]
    (run_dir / "pipeline_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def run_pipeline(pptx_path: str | Path, output_root: str | Path, options: PipelineOptions, log: LogFn | None = None) -> dict[str, object]:
    pptx = Path(pptx_path).expanduser()
    run_dir = _new_run_dir(output_root)
    _log(log, f"创建结果文件夹: {run_dir}")
    copied_pptx = run_dir / "uploaded.pptx"
    shutil.copy2(pptx, copied_pptx)
    extracted_dir = run_dir / "extracted_images"
    images = extract_ppt_images(copied_pptx, extracted_dir)
    _log(log, f"已从 PPTX 提取图片: {len(images)} 张")
    return _run_prepared_images(run_dir, extracted_dir, images, options, input_type="pptx", log=log)


def run_pipeline_from_images(
    image_paths: list[str | Path],
    output_root: str | Path,
    options: PipelineOptions,
    log: LogFn | None = None,
) -> dict[str, object]:
    run_dir = _new_run_dir(output_root)
    _log(log, f"创建结果文件夹: {run_dir}")
    extracted_dir = run_dir / "extracted_images"
    copied = copy_image_inputs(image_paths, extracted_dir)
    manifest = [
        {"order": index, "source_name": Path(source).name, "stored_name": target.name}
        for index, (source, target) in enumerate(zip(image_paths, copied), start=1)
    ]
    (run_dir / "input_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(log, f"已接收直接上传图片: {len(copied)} 张")
    return _run_prepared_images(run_dir, extracted_dir, copied, options, input_type="images", log=log)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Fiji/ImageJ fluorescence quantification from a PPTX.")
    parser.add_argument("pptx")
    parser.add_argument("output_root")
    parser.add_argument("--threshold-method", default="Otsu")
    parser.add_argument("--min-threshold", type=int, default=8)
    parser.add_argument("--order", default="red,green,merge")
    parser.add_argument("--group-names", default="", help="分组名，用英文逗号或换行分隔")
    parser.add_argument("--replicates-per-group", default="3", help="每个实验组的重复次数，不能少于 3")
    parser.add_argument("--roi-per-replicate", type=int, default=3, help="每个生物学重复内测量的 ROI 数")
    parser.add_argument("--expected-trend", default="none", help="预期趋势检查，例如 pbmc_dead_increase；只提示，不改数据")
    parser.add_argument("--trend-min-value", type=float, default=None, help="Dead %趋势范围最小值，仅检查不修改数据")
    parser.add_argument("--trend-max-value", type=float, default=None, help="Dead %趋势范围最大值，仅检查不修改数据")
    parser.add_argument("--threshold-scope", default="fixed", choices=["per_image_otsu", "fixed", "control_fixed"])
    parser.add_argument("--red-fixed-threshold", type=int, default=80)
    parser.add_argument("--green-fixed-threshold", type=int, default=80)
    parser.add_argument("--no-min-threshold", action="store_true")
    parser.add_argument("--no-background-subtraction", action="store_true")
    parser.add_argument("--dead-percent-mode", default="intensity_ratio", choices=["intensity_ratio", "cell_count"])
    parser.add_argument("--fiji-path", default="")
    args = parser.parse_args(argv)

    names_text = args.group_names.replace(",", "\n")
    options = PipelineOptions(
        threshold_method=args.threshold_method,
        min_threshold=args.min_threshold,
        order=parse_order(args.order),
        group_names=parse_group_names(names_text),
        replicates_per_group=parse_replicates_per_group(args.replicates_per_group),
        roi_per_replicate=max(1, args.roi_per_replicate),
        expected_trend=args.expected_trend,
        trend_min_value=args.trend_min_value,
        trend_max_value=args.trend_max_value,
        threshold_scope=args.threshold_scope,
        red_fixed_threshold=args.red_fixed_threshold,
        green_fixed_threshold=args.green_fixed_threshold,
        use_min_threshold=not args.no_min_threshold,
        background_subtraction=not args.no_background_subtraction,
        dead_percent_mode=args.dead_percent_mode,
        fiji_path=args.fiji_path,
    )
    result = run_pipeline(args.pptx, args.output_root, options, log=print)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
