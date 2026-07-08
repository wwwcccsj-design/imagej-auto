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
from .models import CalculatedMeasurement, PipelineOptions
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
    if isinstance(value, float) and math.isnan(value):
        return None
    return round(value, digits)


def _has_mixed_pbmc_groups(groups: list[str]) -> bool:
    has_pbmc = any("pbmc" in group.lower() for group in groups)
    has_no_pbmc = any("pbmc" not in group.lower() for group in groups)
    return has_pbmc and has_no_pbmc


def build_result_payload(
    run_dir: str | Path,
    raw_csv: str | Path,
    calculated: list[CalculatedMeasurement],
    replicates_per_group: int,
    expected_trend: str,
    roi_per_replicate: int = 3,
) -> dict[str, object]:
    run_dir_path = Path(run_dir)
    raw_csv_path = Path(raw_csv)
    repeat_rows = summarize_repeats(calculated)
    summaries = summarize_replicates(calculated)
    trend_check = check_expected_trend(summaries, expected_trend)
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
            "message": trend_check.message,
            "points": [{"group": point.group, "value": _round_or_none(point.value, 4)} for point in trend_check.points],
        },
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


def run_pipeline(pptx_path: str | Path, output_root: str | Path, options: PipelineOptions, log: LogFn | None = None) -> dict[str, object]:
    pptx = Path(pptx_path).expanduser()
    output_root_path = Path(output_root).expanduser()
    selected_trend_label = describe_expected_trend(options.expected_trend)
    output_root_path.mkdir(parents=True, exist_ok=True)
    run_dir = output_root_path / f"imagej_run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    run_dir.mkdir(parents=True, exist_ok=False)

    _log(log, f"创建结果文件夹: {run_dir}")
    _log(log, f"用户预期趋势: {selected_trend_label}")
    copied_pptx = run_dir / "uploaded.pptx"
    shutil.copy2(pptx, copied_pptx)

    extracted_dir = run_dir / "extracted_images"
    images = extract_ppt_images(copied_pptx, extracted_dir)
    _log(log, f"已提取图片: {len(images)} 张")
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
        },
    )
    result = build_result_payload(
        run_dir,
        raw_csv,
        calculated,
        options.replicates_per_group,
        options.expected_trend,
        roi_per_replicate=options.roi_per_replicate,
    )
    _log(log, "Excel、Prism 数据、SVG 图和 HTML 报告已生成")
    _log(log, f"趋势检查: {result['trend']['message']}")  # type: ignore[index]
    (run_dir / "pipeline_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


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
