from __future__ import annotations

from .models import ImageTriplet


def _macro_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _array(values: list[str]) -> str:
    return "newArray(" + ", ".join(_macro_quote(value) for value in values) + ")"


def build_macro(
    triplets: list[ImageTriplet],
    threshold_method: str = "Otsu",
    min_threshold: int = 8,
    repeat_count: int = 3,
    roi_per_repeat: int = 3,
    threshold_scope: str = "fixed",
    red_fixed_threshold: int = 80,
    green_fixed_threshold: int = 80,
    use_min_threshold: bool = True,
    background_subtraction: bool = True,
    dead_percent_mode: str = "intensity_ratio",
    replicate_count: int | None = None,
) -> str:
    if not triplets:
        raise ValueError("没有可写入 ImageJ 宏的分组。")
    if replicate_count is not None:
        repeat_count = replicate_count
        roi_per_repeat = 1
    groups = [t.group for t in triplets]
    red_files = [t.red.name for t in triplets]
    green_files = [t.green.name for t in triplets]
    merge_files = [t.merge.name for t in triplets]
    min_threshold = max(0, min(254, int(min_threshold)))
    repeat_count = max(1, int(repeat_count))
    roi_per_repeat = max(1, int(roi_per_repeat))
    red_fixed_threshold = max(0, min(254, int(red_fixed_threshold)))
    green_fixed_threshold = max(0, min(254, int(green_fixed_threshold)))
    use_min_threshold_value = 1 if use_min_threshold else 0
    background_subtraction_value = 1 if background_subtraction else 0

    return f"""// Auto-generated Fiji/ImageJ fluorescence quantification macro.
// PI red image = dead signal; Calcein-AM green image = live signal.
// Trend checks are report-only and never change ImageJ measurements.

inputDir = getArgument();
if (inputDir == "") {{
    inputDir = getDirectory("Choose extracted_images folder");
}}
if (!endsWith(inputDir, File.separator)) {{
    inputDir = inputDir + File.separator;
}}

outputDir = inputDir + ".." + File.separator;
maskDir = outputDir + "imagej_masks" + File.separator;
File.makeDirectory(maskDir);

thresholdMethod = {_macro_quote(threshold_method)};
thresholdScope = {_macro_quote(threshold_scope)};
deadPercentMode = {_macro_quote(dead_percent_mode)};
minThreshold = {min_threshold};
redFixedThreshold = {red_fixed_threshold};
greenFixedThreshold = {green_fixed_threshold};
useMinThreshold = {use_min_threshold_value};
backgroundSubtraction = {background_subtraction_value};
repeatCount = {repeat_count};
roiPerRepeat = {roi_per_repeat};
totalRois = repeatCount * roiPerRepeat;
groups = {_array(groups)};
redFiles = {_array(red_files)};
greenFiles = {_array(green_files)};
mergeFiles = {_array(merge_files)};

controlRedThreshold = redFixedThreshold;
controlGreenThreshold = greenFixedThreshold;
if (thresholdScope == "control_fixed") {{
    controlRedThreshold = estimateThreshold(inputDir + redFiles[0], "red");
    controlGreenThreshold = estimateThreshold(inputDir + greenFiles[0], "green");
}}

csv = "group,biological_repeat,roi_number,red_image,green_image,merge_image,roi_label,roi_x,roi_y,roi_radius,roi_area,red_threshold,green_threshold,red_positive_pixels,green_positive_pixels,red_mean_positive_intensity,green_mean_positive_intensity,red_raw_integrated_density,red_positive_intensity_sum,red_background_mean,red_corrected_intensity_raw,red_saturated_pixel_percent,green_raw_integrated_density,green_positive_intensity_sum,green_background_mean,green_corrected_intensity_raw,green_saturated_pixel_percent,saturated_pixel_percent\\n";
setBatchMode(true);

for (i = 0; i < groups.length; i++) {{
    for (rep = 0; rep < repeatCount; rep++) {{
        groupLabel = groups[i] + " R" + (rep + 1);
        for (roiIndex = 0; roiIndex < roiPerRepeat; roiIndex++) {{
            roiGlobalIndex = rep * roiPerRepeat + roiIndex;
            roiLabel = "ROI" + (roiIndex + 1);
            maskBase = safeName(groupLabel + "_" + roiLabel);
            saveRoiOverlay(inputDir + mergeFiles[i], maskDir + maskBase + "_ROI_overlay.png", roiGlobalIndex, totalRois);
            red = measureChannel(inputDir + redFiles[i], "red", maskDir + maskBase + "_PI_red_mask.png", roiGlobalIndex, totalRois);
            green = measureChannel(inputDir + greenFiles[i], "green", maskDir + maskBase + "_Calcein_green_mask.png", roiGlobalIndex, totalRois);
            saturated = red[10];
            if (green[10] > saturated) saturated = green[10];
            csv += groupLabel + "," + "R" + (rep + 1) + "," + roiLabel + "," +
                redFiles[i] + "," + greenFiles[i] + "," + mergeFiles[i] + "," +
                roiLabel + "," + red[3] + "," + red[4] + "," + red[5] + "," + red[6] + "," +
                red[0] + "," + green[0] + "," + red[1] + "," + green[1] + "," + red[2] + "," + green[2] + "," +
                red[7] + "," + red[11] + "," + red[8] + "," + red[9] + "," + red[10] + "," +
                green[7] + "," + green[11] + "," + green[8] + "," + green[9] + "," + green[10] + "," + saturated + "\\n";
        }}
    }}
}}

File.saveString(csv, outputDir + "imagej_raw_measurements.csv");
setBatchMode(false);
print("Saved: " + outputDir + "imagej_raw_measurements.csv");
run("Quit");

function measureChannel(path, channel, maskPath, roiIndex, totalRois) {{
    open(path);
    original = getTitle();
    run("Split Channels");
    if (channel == "red") {{
        target = original + " (red)";
        fixedThreshold = redFixedThreshold;
        controlThreshold = controlRedThreshold;
        closeIfOpen(original + " (green)");
        closeIfOpen(original + " (blue)");
    }} else {{
        target = original + " (green)";
        fixedThreshold = greenFixedThreshold;
        controlThreshold = controlGreenThreshold;
        closeIfOpen(original + " (red)");
        closeIfOpen(original + " (blue)");
    }}

    selectWindow(target);
    run("8-bit");
    width = getWidth();
    height = getHeight();
    roi = roiForIndex(roiIndex, totalRois, width, height);
    makeOval(roi[0], roi[1], roi[2] * 2, roi[2] * 2);
    getStatistics(roiArea, roiMean, roiMin, roiMax, roiStd);
    rawIntegrated = roiArea * roiMean;

    if (thresholdScope == "per_image_otsu") {{
        setAutoThreshold(thresholdMethod + " dark");
        getThreshold(lower, upper);
        lower = floor(lower);
    }} else if (thresholdScope == "control_fixed") {{
        lower = controlThreshold;
    }} else {{
        lower = fixedThreshold;
    }}
    if (useMinThreshold == 1 && lower < minThreshold) lower = minThreshold;
    if (lower < 0) lower = 0;
    if (lower > 254) lower = 254;
    setThreshold(lower + 1, 255);

    getHistogram(values, counts, 256);
    positivePixelCount = 0;
    positiveIntensitySum = 0;
    backgroundPixelCount = 0;
    backgroundIntensitySum = 0;
    saturatedPixels = counts[255];
    for (j = 0; j <= lower; j++) {{
        backgroundPixelCount += counts[j];
        backgroundIntensitySum += j * counts[j];
    }}
    for (j = lower + 1; j < 256; j++) {{
        positivePixelCount += counts[j];
        positiveIntensitySum += j * counts[j];
    }}

    meanPositive = 0;
    if (positivePixelCount > 0) meanPositive = positiveIntensitySum / positivePixelCount;
    backgroundMean = 0;
    if (backgroundSubtraction == 1 && backgroundPixelCount > 0) {{
        backgroundMean = backgroundIntensitySum / backgroundPixelCount;
    }}
    correctedRaw = positiveIntensitySum - positivePixelCount * backgroundMean;
    if (correctedRaw < 0) correctedRaw = 0;
    saturatedPercent = 0;
    if (roiArea > 0) saturatedPercent = saturatedPixels / roiArea * 100;

    run("Convert to Mask");
    saveAs("PNG", maskPath);
    close();

    result = newArray(12);
    result[0] = lower;
    result[1] = positivePixelCount;
    result[2] = meanPositive;
    result[3] = roi[0] + roi[2];
    result[4] = roi[1] + roi[2];
    result[5] = roi[2];
    result[6] = roiArea;
    result[7] = rawIntegrated;
    result[8] = backgroundMean;
    result[9] = correctedRaw;
    result[10] = saturatedPercent;
    result[11] = positiveIntensitySum;
    return result;
}}

function saveRoiOverlay(path, overlayPath, roiIndex, totalRois) {{
    open(path);
    title = getTitle();
    width = getWidth();
    height = getHeight();
    roi = roiForIndex(roiIndex, totalRois, width, height);
    makeOval(roi[0], roi[1], roi[2] * 2, roi[2] * 2);
    setLineWidth(4);
    setColor("yellow");
    run("Draw");
    saveAs("PNG", overlayPath);
    close();
}}

function estimateThreshold(path, channel) {{
    open(path);
    original = getTitle();
    run("Split Channels");
    if (channel == "red") {{
        target = original + " (red)";
        closeIfOpen(original + " (green)");
        closeIfOpen(original + " (blue)");
    }} else {{
        target = original + " (green)";
        closeIfOpen(original + " (red)");
        closeIfOpen(original + " (blue)");
    }}
    selectWindow(target);
    run("8-bit");
    setAutoThreshold(thresholdMethod + " dark");
    getThreshold(lower, upper);
    lower = floor(lower);
    if (useMinThreshold == 1 && lower < minThreshold) lower = minThreshold;
    if (lower < 0) lower = 0;
    if (lower > 254) lower = 254;
    close();
    return lower;
}}

function roiForIndex(index, total, width, height) {{
    radius = floor(minOf(width, height) / 10);
    if (radius < 8) radius = floor(minOf(width, height) / 4);
    if (radius < 1) radius = 1;

    columns = 3;
    rows = floor((total + columns - 1) / columns);
    row = floor(index / columns);
    col = index % columns;
    centerX = floor(width * (col + 1) / (columns + 1));
    centerY = floor(height * (row + 1) / (rows + 1));

    x = centerX - radius;
    y = centerY - radius;
    if (x < 0) x = 0;
    if (y < 0) y = 0;
    if (x + radius * 2 > width) x = width - radius * 2;
    if (y + radius * 2 > height) y = height - radius * 2;
    if (x < 0) x = 0;
    if (y < 0) y = 0;

    roi = newArray(3);
    roi[0] = x;
    roi[1] = y;
    roi[2] = radius;
    return roi;
}}

function closeIfOpen(title) {{
    if (isOpen(title)) {{
        selectWindow(title);
        close();
    }}
}}

function safeName(name) {{
    s = replace(name, " ", "_");
    s = replace(s, "+", "_");
    s = replace(s, "(", "");
    s = replace(s, ")", "");
    s = replace(s, "/", "_");
    s = replace(s, "\\\\", "_");
    s = replace(s, ",", "_");
    s = replace(s, "，", "_");
    return s;
}}
"""
