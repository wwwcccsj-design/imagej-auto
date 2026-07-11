import csv
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from imagej_auto.grouping import build_triplets, parse_replicates_per_group
from imagej_auto.imagej_macro import build_macro
from imagej_auto.imagej_runner import run_imagej_macro
from imagej_auto.models import PipelineOptions, RawMeasurement, ReplicateSummary
from imagej_auto.pipeline import (
    build_result_payload,
    copy_image_inputs,
    detect_montage_layout,
    prepare_direct_image_inputs,
    run_pipeline_from_images,
    validate_threshold_results,
)
from imagej_auto.ppt_extractor import natural_key
from imagej_auto.report_builder import (
    calculate_measurement,
    check_expected_trend,
    write_roi_level_csv,
    summarize_repeats,
    summarize_replicates,
    write_formula_workbook,
    write_prism_files,
)


class CoreWorkflowTests(unittest.TestCase):
    @staticmethod
    def _threshold_raw(red: int = 80, green: int = 80) -> RawMeasurement:
        return RawMeasurement("CON R1", "r.png", "g.png", "m.png", red, green, 1, 1, 1.0, 1.0)

    def test_natural_key_sorts_powerpoint_image_names(self):
        names = ["image10.png", "image2.png", "image1.png", "image16.jpg"]
        self.assertEqual(sorted(names, key=natural_key), ["image1.png", "image2.png", "image10.png", "image16.jpg"])

    def test_build_triplets_uses_selected_order_and_group_names(self):
        images = [Path(f"image{i}.png") for i in range(1, 7)]
        triplets = build_triplets(images, group_names=["CON", "AUR"], order=("red", "green", "merge"))
        self.assertEqual([t.group for t in triplets], ["CON", "AUR"])
        self.assertEqual(triplets[0].red.name, "image1.png")
        self.assertEqual(triplets[0].green.name, "image2.png")
        self.assertEqual(triplets[1].red.name, "image4.png")

    def test_build_triplets_keeps_each_image_triplet_as_a_group_when_repeats_are_roi_based(self):
        images = [Path(f"image{i}.png") for i in range(1, 19)]
        triplets = build_triplets(
            images,
            group_names=["CON", "AUR2 uM", "AUR4 uM", "CON+PBMC", "AUR(2uM)+PBMC", "AUR(4uM)+PBMC"],
            order=("red", "green", "merge"),
            replicates_per_group=3,
        )
        self.assertEqual(len(triplets), 6)
        self.assertEqual(triplets[5].red.name, "image16.png")

    def test_parse_replicates_per_group_requires_at_least_three(self):
        self.assertEqual(parse_replicates_per_group("3"), 3)
        with self.assertRaises(ValueError):
            parse_replicates_per_group("2")

    def test_direct_image_copy_preserves_selected_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sources = []
            for index, name in enumerate(("z_red.png", "a_green.jpg", "m_merge.tif"), start=1):
                path = root / name
                path.write_bytes(f"source-{index}".encode())
                sources.append(path)
            copied = copy_image_inputs(sources, root / "extracted")
            self.assertEqual([path.name for path in copied], ["image0001.png", "image0002.jpg", "image0003.tif"])
            self.assertEqual([path.read_bytes() for path in copied], [b"source-1", b"source-2", b"source-3"])

    def test_direct_image_pipeline_uses_shared_analysis_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sources = []
            for index in range(3):
                path = root / f"source{index}.png"
                path.write_bytes(b"image")
                sources.append(path)
            with patch("imagej_auto.pipeline._run_prepared_images", return_value={"ok": True}) as run_mock:
                result = run_pipeline_from_images(sources, root / "out", PipelineOptions(image_layout="triplets"))
            self.assertTrue(result["ok"])
            self.assertEqual(result["image_layout_detection"]["mode"], "triplets")
            self.assertEqual(run_mock.call_args.kwargs["input_type"], "images")
            self.assertTrue(run_mock.call_args.args[0].joinpath("input_manifest.json").exists())

    def test_montage_detection_splits_six_groups_and_three_channels(self):
        from PIL import Image, ImageDraw

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "montage.png"
            image = Image.new("RGB", (720, 390), "white")
            draw = ImageDraw.Draw(image)
            for column in range(6):
                for row in range(3):
                    left = 10 + column * 118
                    top = 15 + row * 125
                    draw.rectangle((left, top, left + 95, top + 100), fill=(5, 5, 5))
            image.save(source)

            detected = detect_montage_layout(source)
            self.assertIsNotNone(detected)
            self.assertEqual(detected["columns"], 6)
            self.assertEqual(detected["rows"], 3)

            cropped, metadata = prepare_direct_image_inputs([source], root / "extracted", image_layout="auto")
            self.assertEqual(len(cropped), 18)
            self.assertEqual(metadata["mode"], "montage")
            self.assertEqual(metadata["detected_columns"], 6)

    def test_fixed_threshold_validation_checks_actual_imagej_values(self):
        options = PipelineOptions(
            threshold_scope="fixed",
            red_fixed_threshold=80,
            green_fixed_threshold=80,
            use_min_threshold=False,
        )
        validation = validate_threshold_results([self._threshold_raw()], options)
        self.assertTrue(validation["passed"])
        self.assertEqual(validation["red_values"], [80])
        self.assertEqual(validation["green_values"], [80])

        with self.assertRaisesRegex(RuntimeError, "固定阈值未生效"):
            validate_threshold_results([self._threshold_raw(red=79)], options)

    def test_trend_bounds_warn_without_modifying_summary_values(self):
        summaries = [
            ReplicateSummary("CON", 3, 0.2, 0.01, 10.0, 1.0, 90.0, 1.0),
            ReplicateSummary("AUR2 uM", 3, 0.5, 0.02, 40.0, 2.0, 60.0, 2.0),
        ]
        before = [(row.group, row.dead_percent_mean) for row in summaries]
        check = check_expected_trend(summaries, "none", min_value=20, max_value=60)
        self.assertFalse(check.passed)
        self.assertIn("CON=10.00<最小值20.00", check.message)
        self.assertIn("不会优化、裁剪或修改数据", check.message)
        self.assertEqual(before, [(row.group, row.dead_percent_mean) for row in summaries])

    def test_calculate_measurement_uses_corrected_intensity_for_dead_percent(self):
        raw = RawMeasurement(
            group="CON R1",
            red_image="image1.png",
            green_image="image2.png",
            merge_image="image3.png",
            red_threshold_otsu=93,
            green_threshold_otsu=111,
            red_positive_pixels=20,
            green_positive_pixels=80,
            red_mean_positive_intensity=180.0,
            green_mean_positive_intensity=220.0,
            roi_area=100.0,
            red_raw_integrated_density=3000.0,
            red_background_mean=5.0,
            green_raw_integrated_density=7000.0,
            green_background_mean=10.0,
        )
        calc = calculate_measurement(raw)
        self.assertAlmostEqual(calc.red_corrected_intensity, 2500.0)
        self.assertAlmostEqual(calc.green_corrected_intensity, 6000.0)
        self.assertAlmostEqual(calc.dead_percent, 2500.0 / 8500.0 * 100)

    def test_negative_corrected_intensity_is_clipped_and_qc_logged(self):
        raw = RawMeasurement(
            group="CON R1",
            red_image="r.png",
            green_image="g.png",
            merge_image="m.png",
            red_threshold_otsu=10,
            green_threshold_otsu=20,
            red_positive_pixels=1,
            green_positive_pixels=1,
            red_mean_positive_intensity=10.0,
            green_mean_positive_intensity=10.0,
            roi_area=100.0,
            red_raw_integrated_density=10.0,
            red_background_mean=1.0,
            green_raw_integrated_density=1000.0,
            green_background_mean=1.0,
        )
        calc = calculate_measurement(raw)
        self.assertEqual(calc.red_corrected_intensity, 0.0)
        self.assertIn("红色通道背景扣除后为负值", calc.qc_warning)

    def test_red_positive_green_zero_is_not_reported_as_zero_ratio(self):
        raw = RawMeasurement(
            "CON R1",
            "r.png",
            "g.png",
            "m.png",
            80,
            80,
            5,
            0,
            10.0,
            0.0,
            red_positive_intensity_sum=100.0,
            green_positive_intensity_sum=0.0,
            red_corrected_intensity_raw=100.0,
            green_corrected_intensity_raw=0.0,
        )
        calc = calculate_measurement(raw)
        self.assertTrue(calc.red_green_ratio > 0)
        self.assertAlmostEqual(calc.dead_percent, 100.0)
        self.assertAlmostEqual(calc.live_percent, 0.0)
        self.assertIn("绿色校正信号为0", calc.qc_warning)

    def test_red_zero_green_zero_returns_na_percentages(self):
        raw = RawMeasurement(
            "CON R1",
            "r.png",
            "g.png",
            "m.png",
            80,
            80,
            0,
            0,
            0.0,
            0.0,
            red_positive_intensity_sum=0.0,
            green_positive_intensity_sum=0.0,
            red_corrected_intensity_raw=0.0,
            green_corrected_intensity_raw=0.0,
        )
        calc = calculate_measurement(raw)
        self.assertTrue(calc.red_green_ratio != calc.red_green_ratio)
        self.assertTrue(calc.dead_percent != calc.dead_percent)
        self.assertTrue(calc.live_percent != calc.live_percent)

    def test_repeat_level_averages_rois_before_group_summary(self):
        raws = [
            RawMeasurement("CON R1", "r1.png", "g1.png", "m1.png", 10, 20, 1, 1, 1.0, 1.0, roi_label="ROI1", red_corrected_intensity_raw=20, green_corrected_intensity_raw=80),
            RawMeasurement("CON R1", "r1.png", "g1.png", "m1.png", 10, 20, 1, 1, 1.0, 1.0, roi_label="ROI2", red_corrected_intensity_raw=40, green_corrected_intensity_raw=60),
            RawMeasurement("CON R1", "r1.png", "g1.png", "m1.png", 10, 20, 1, 1, 1.0, 1.0, roi_label="ROI3", red_corrected_intensity_raw=60, green_corrected_intensity_raw=40),
            RawMeasurement("CON R2", "r2.png", "g2.png", "m2.png", 10, 20, 1, 1, 1.0, 1.0, roi_label="ROI1", red_corrected_intensity_raw=20, green_corrected_intensity_raw=80),
            RawMeasurement("CON R3", "r3.png", "g3.png", "m3.png", 10, 20, 1, 1, 1.0, 1.0, roi_label="ROI1", red_corrected_intensity_raw=60, green_corrected_intensity_raw=40),
        ]
        calculated = [calculate_measurement(raw) for raw in raws]
        repeat_summary = summarize_repeats(calculated)
        group_summary = summarize_replicates(calculated)

        self.assertEqual(repeat_summary[0].valid_roi_count, 3)
        self.assertAlmostEqual(repeat_summary[0].dead_percent, 40.0)
        self.assertEqual(group_summary[0].replicate_count, 3)
        self.assertAlmostEqual(group_summary[0].dead_percent_mean, 40.0)

    def test_pbmc_trend_is_checked_as_two_separate_series(self):
        values = {
            "CON R1": 23.41,
            "AUR2 uM R1": 26.70,
            "AUR4 uM R1": 20.25,
            "CON+PBMC R1": 11.90,
            "AUR(2uM)+PBMC R1": 45.51,
            "AUR(4uM)+PBMC R1": 40.52,
        }
        calculated = [
            calculate_measurement(
                RawMeasurement(
                    group=group,
                    red_image="r.png",
                    green_image="g.png",
                    merge_image="m.png",
                    red_threshold_otsu=10,
                    green_threshold_otsu=20,
                    red_positive_pixels=1,
                    green_positive_pixels=1,
                    red_mean_positive_intensity=1.0,
                    green_mean_positive_intensity=1.0,
                    red_corrected_intensity_raw=dead,
                    green_corrected_intensity_raw=100 - dead,
                )
            )
            for group, dead in values.items()
        ]
        trend = check_expected_trend(summarize_replicates(calculated), "pbmc_dead_increase")
        self.assertFalse(trend.passed)
        self.assertIn("无PBMC", trend.message)
        self.assertIn("PBMC", trend.message)
        self.assertIn("第3组较第2组降低", trend.message)

    def test_result_payload_exposes_repeat_level_without_mutation_flag(self):
        raws = [
            RawMeasurement("CON R1", "r1.png", "g1.png", "m1.png", 10, 20, 1, 1, 1.0, 1.0, red_corrected_intensity_raw=20, green_corrected_intensity_raw=80),
            RawMeasurement("CON R2", "r2.png", "g2.png", "m2.png", 10, 20, 1, 1, 1.0, 1.0, red_corrected_intensity_raw=40, green_corrected_intensity_raw=60),
            RawMeasurement("CON R3", "r3.png", "g3.png", "m3.png", 10, 20, 1, 1, 1.0, 1.0, red_corrected_intensity_raw=60, green_corrected_intensity_raw=40),
        ]
        calculated = [calculate_measurement(raw) for raw in raws]
        payload = build_result_payload(
            Path("/tmp/imagej_run_test"),
            Path("/tmp/imagej_run_test/imagej_raw_measurements.csv"),
            calculated,
            replicates_per_group=3,
            expected_trend="none",
            roi_per_replicate=3,
        )
        self.assertNotIn("force" + "_trend", payload)
        self.assertEqual(payload["summary"][0]["replicate_count"], 3)
        self.assertEqual(len(payload["repeat_level"]), 3)

    def test_write_prism_files_contains_three_repeat_rows_and_mean_appendix(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            raws = [
                RawMeasurement("CON R1", "r1.png", "g1.png", "m1.png", 10, 20, 1, 1, 1.0, 1.0, red_corrected_intensity_raw=20, green_corrected_intensity_raw=80),
                RawMeasurement("CON R2", "r2.png", "g2.png", "m2.png", 10, 20, 1, 1, 1.0, 1.0, red_corrected_intensity_raw=40, green_corrected_intensity_raw=60),
                RawMeasurement("CON R3", "r3.png", "g3.png", "m3.png", 10, 20, 1, 1, 1.0, 1.0, red_corrected_intensity_raw=60, green_corrected_intensity_raw=40),
            ]
            write_prism_files([calculate_measurement(raw) for raw in raws], out)
            with (out / "Prism_RedGreen_ratio_column.csv").open(newline="", encoding="utf-8") as f:
                ratio_rows = list(csv.reader(f))
            with (out / "Prism_DeadLive_percent_grouped.csv").open(newline="", encoding="utf-8") as f:
                dead_live_rows = list(csv.reader(f))
            self.assertEqual(ratio_rows[0], ["Group", "Biological_repeat", "Red_Green_ratio"])
            self.assertEqual(ratio_rows[1][0:2], ["CON", "R1"])
            self.assertEqual(ratio_rows[3][0:2], ["CON", "R3"])
            self.assertIn(["三组重复平均值"], ratio_rows)
            self.assertIn(["Group", "Red_Green_ratio_mean", "Red_Green_ratio_SD", "Biological_repeats"], ratio_rows)
            self.assertIn(["三组重复平均值"], dead_live_rows)
            self.assertIn(["Group", "Dead_percent_mean", "Dead_percent_SD", "Live_percent_mean", "Live_percent_SD", "Biological_repeats"], dead_live_rows)

    def test_build_macro_contains_repeat_roi_and_threshold_settings(self):
        images = [Path(f"image{i}.png") for i in range(1, 4)]
        triplet = build_triplets(images, group_names=["CON"], order=("red", "green", "merge"))[0]
        macro = build_macro(
            [triplet],
            threshold_method="Otsu",
            min_threshold=8,
            repeat_count=3,
            roi_per_repeat=3,
            threshold_scope="fixed",
            red_fixed_threshold=80,
            green_fixed_threshold=80,
        )
        self.assertIn("repeatCount = 3;", macro)
        self.assertIn("roiPerRepeat = 3;", macro)
        self.assertIn('thresholdScope = "fixed";', macro)
        self.assertIn("redFixedThreshold = 80;", macro)
        self.assertIn("greenFixedThreshold = 80;", macro)
        self.assertIn('roiLabel = "ROI" + (roiIndex + 1);', macro)
        self.assertIn("red_corrected_intensity_raw", macro)
        self.assertIn("controlRedThreshold = estimateThreshold", macro)
        self.assertIn("positiveIntensitySum", macro)
        self.assertIn("positivePixelCount", macro)
        self.assertIn("backgroundIntensitySum", macro)
        self.assertNotIn('run("Make Inverse");', macro)
        self.assertNotIn("correctedRaw = rawIntegrated - roiArea * backgroundMean;", macro)
        self.assertIn("correctedRaw = positiveIntensitySum - positivePixelCount * backgroundMean;", macro)
        self.assertIn("for (j = 0; j <= lower; j++)", macro)
        self.assertIn("result = newArray(12);", macro)

    def test_roi_level_csv_contains_required_fields_without_column_shift(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = RawMeasurement(
                "CON R1",
                "r.png",
                "g.png",
                "m.png",
                80,
                80,
                5,
                7,
                10.0,
                12.0,
                roi_label="ROI1",
                roi_x=10,
                roi_y=11,
                roi_radius=12,
                roi_area=400.0,
                red_raw_integrated_density=1000.0,
                red_positive_intensity_sum=800.0,
                red_background_mean=10.0,
                red_corrected_intensity_raw=750.0,
                green_raw_integrated_density=900.0,
                green_positive_intensity_sum=700.0,
                green_background_mean=8.0,
                green_corrected_intensity_raw=644.0,
                red_saturated_pixel_percent=0.0,
                green_saturated_pixel_percent=0.0,
            )
            out = write_roi_level_csv([calculate_measurement(raw)], Path(tmp))
            with out.open(newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(rows[0]["Biological_repeat"], "R1")
            self.assertEqual(rows[0]["ROI_number"], "ROI1")
            self.assertEqual(rows[0]["Red_threshold"], "80")
            self.assertEqual(rows[0]["Green_threshold"], "80")
            self.assertEqual(rows[0]["Red_positive_intensity_sum"], "800.000000")
            self.assertEqual(rows[0]["Green_positive_intensity_sum"], "700.000000")

    def test_run_imagej_macro_passes_absolute_paths_to_fiji(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
            run_dir = Path(tmp)
            images = run_dir / "extracted_images"
            images.mkdir()
            macro = run_dir / "macro.ijm"
            macro.write_text("// macro", encoding="utf-8")
            relative_run_dir = run_dir.relative_to(Path.cwd())
            relative_macro = macro.relative_to(Path.cwd())
            relative_images = images.relative_to(Path.cwd())

            with (
                patch("imagej_auto.imagej_runner.find_fiji", return_value="/usr/local/bin/fiji"),
                patch("imagej_auto.imagej_runner.subprocess.run") as run_mock,
            ):
                run_mock.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
                run_imagej_macro(relative_macro, relative_images, relative_run_dir)

            command = run_mock.call_args.args[0]
            self.assertNotIn("--console", command)
            self.assertEqual(command[3], str(macro.resolve()))
            self.assertEqual(command[4], str(images.resolve()))
            self.assertEqual(run_mock.call_args.kwargs["cwd"], run_dir.resolve())

    def test_write_formula_workbook_includes_repeat_and_group_formulas(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "results.xlsx"
            raws = [
                RawMeasurement("CON R1", "r1.png", "g1.png", "m1.png", 10, 20, 1, 1, 1.0, 1.0, red_corrected_intensity_raw=20, green_corrected_intensity_raw=80),
                RawMeasurement("CON R2", "r2.png", "g2.png", "m2.png", 10, 20, 1, 1, 1.0, 1.0, red_corrected_intensity_raw=40, green_corrected_intensity_raw=60),
                RawMeasurement("CON R3", "r3.png", "g3.png", "m3.png", 10, 20, 1, 1, 1.0, 1.0, red_corrected_intensity_raw=60, green_corrected_intensity_raw=40),
            ]
            write_formula_workbook(raws, out)
            self.assertTrue(out.exists())
            with zipfile.ZipFile(out) as zf:
                repeat_sheet = zf.read("xl/worksheets/sheet4.xml").decode("utf-8")
                group_sheet = zf.read("xl/worksheets/sheet5.xml").decode("utf-8")
            self.assertIn("<f>AVERAGE('Calculations'!E2)</f>", repeat_sheet)
            self.assertIn("<f>STDEV.S('Repeat Means'!G2,'Repeat Means'!G3,'Repeat Means'!G4)</f>", group_sheet)


if __name__ == "__main__":
    unittest.main()
