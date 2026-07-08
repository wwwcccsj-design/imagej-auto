# ImageJ Auto Fluorescence Quantification

一个用于 **PPTX 荧光图片自动提取、Fiji/ImageJ 批量测量、Live/Dead 结果汇总和 GraphPad Prism 数据导出** 的本地网页工具。

项目最初用于 PI red / Calcein-AM green 的活死染分析，但代码结构也适合其它红绿荧光双通道场景。工具强调可审计计算：不重排实验组、不删除异常值、不强行制造趋势，趋势判断只作为报告提示。

## 功能概览

- 从 `.pptx` 中按自然顺序提取图片。
- 按红图、绿图、合并图三张一组形成实验组。
- 通过 Fiji/ImageJ headless 生成 ROI、mask 和测量 CSV。
- 支持三种阈值模式：
  - `fixed`：整批固定阈值，红绿通道分别使用用户输入阈值。
  - `per_image_otsu`：每张图片独立自动阈值。
  - `control_fixed`：用对照组估计阈值后应用到整批。
- 背景扣除只使用当前圆形 ROI 内阈值及以下像素，不使用整张图 ROI 外背景。
- 支持 `生物学重复 -> ROI` 分层统计：
  - 每个生物学重复内多个 ROI 先求均值。
  - 再用 R1/R2/R3 等重复均值计算组 mean 和 sample SD。
- 输出 ROI 级、重复级、组级 CSV。
- 输出 GraphPad Prism 可直接导入的表格。
- 输出带公式的 Excel 工作簿、HTML 报告、SVG 图和 QC 报告。
- 趋势检查按 PBMC 状态分别判断，不会修改原始数据或结果。

## 目录结构

```text
.
├── run_app.py                    # 本地网页入口
├── web/index.html                # 前端页面
├── src/imagej_auto/
│   ├── server.py                 # HTTP 服务和表单解析
│   ├── pipeline.py               # PPT 提取、宏生成、Fiji 调用和报告流程
│   ├── imagej_macro.py           # ImageJ 宏模板生成源码
│   ├── imagej_runner.py          # Fiji headless 调用
│   ├── report_builder.py         # 计算、汇总、导出
│   ├── grouping.py               # 图片分组
│   └── models.py                 # 数据模型
├── tests/                        # 自动测试
└── 启动.command                  # macOS 双击启动脚本
```

## 运行环境

推荐环境：

- macOS
- Python 3.11 或更新版本
- Fiji/ImageJ

Python 运行时主要使用标准库；测试需要 `pytest`。

### 安装 Fiji/ImageJ

macOS 推荐安装 Fiji，并确保命令行可执行文件存在：

```bash
/usr/local/bin/fiji
```

如果 Fiji 在其它位置，可以在网页中的 `Fiji/ImageJ 路径` 一栏填写完整路径，例如：

```text
/Applications/Fiji.app/Contents/MacOS/ImageJ-macosx
```

不同 Fiji 安装包的可执行文件名可能不同，填写实际可执行文件即可。

## 快速开始

克隆仓库：

```bash
git clone https://github.com/wwwcccsj-design/imagej-auto.git
cd imagej-auto
```

启动网页：

```bash
python3 run_app.py
```

终端会显示类似：

```text
ImageJ 自动化检测已启动: http://127.0.0.1:8765/
```

浏览器打开该地址即可使用。macOS 用户也可以双击：

```text
启动.command
```

## 输入 PPTX 的图片顺序

工具默认把 PPTX 中提取出的图片按自然顺序排序，并按三张一组解析：

```text
红色死亡图 / 绿色存活图 / 合并图
```

如果你的 PPT 图片顺序不同，可以在页面中调整 `图片顺序`。

常见顺序示例：

```text
image1 = red
image2 = green
image3 = merge
image4 = red
image5 = green
image6 = merge
...
```

## 推荐实验分组格式

页面默认提供 6 组 AUR/PBMC 示例：

```text
CON
AUR2 uM
AUR4 uM
CON+PBMC
AUR(2uM)+PBMC
AUR(4uM)+PBMC
```

每行一个组名。工具不会重排这些组，也不会为了趋势检查交换组别。

## 重复和 ROI 规则

默认设置：

```text
每组重复次数 = 3
每个重复 ROI 数 = 3
```

统计层级为：

```text
每个生物学重复内的 ROI1/ROI2/ROI3
    -> 先求该重复的均值

R1/R2/R3
    -> 再计算实验组 mean 和 sample SD
```

重要原则：

- 不把 9 个 ROI 当作 9 个生物学重复。
- 组 SD 基于 R1/R2/R3 等重复均值计算。
- SD 使用样本标准差，即 `ddof=1`。
- 有效 ROI 少于预设数量时，会写入 QC 报告。

## 阈值模式

### 整批固定阈值 fixed

适合用户已经确定红绿通道统一阈值的情况。

页面默认：

```text
红色固定阈值 = 80
绿色固定阈值 = 80
```

生成的 ImageJ 宏中会写入：

```ijm
thresholdScope = "fixed";
redFixedThreshold = 80;
greenFixedThreshold = 80;
```

在该模式下，不会用 CON 组 Otsu 覆盖用户输入阈值。

### 每张图片独立 Otsu per_image_otsu

每张图片各自自动估计阈值。适合曝光条件差异较大、需要逐图独立判断的场景。

### 对照组阈值应用整批 control_fixed

使用第一组对照图估计红绿阈值，然后应用到整批图片。适合对照组代表性较好、希望整批阈值统一但不手填阈值的场景。

## 背景扣除逻辑

每个通道只分析当前圆形 ROI 内部像素。

当前 ROI 内：

- 像素值 `<= threshold`：作为局部背景候选。
- 像素值 `> threshold`：作为阳性荧光信号。

核心计算：

```text
backgroundMean =
  阈值及以下像素强度总和 / 阈值及以下像素数量

positivePixelCount =
  阈值以上像素数量

positiveIntensitySum =
  阈值以上像素强度总和

corrected =
  positiveIntensitySum - positivePixelCount * backgroundMean
```

如果 `corrected < 0`，后续计算中按 `0` 处理，并写入 QC 提示。

工具不会使用 `Make Inverse` 把整张图 ROI 外区域当背景。

## Dead%、Live% 和 Red/Green 计算

设：

```text
red = max(red_corrected, 0)
green = max(green_corrected, 0)
```

计算规则：

| 情况 | Red/Green | Dead % | Live % | QC |
|---|---:|---:|---:|---|
| red > 0 且 green > 0 | red / green | red / (red + green) * 100 | 100 - Dead % | - |
| red > 0 且 green == 0 | Infinity / NA | 100 | 0 | 绿色校正信号为0 |
| red == 0 且 green > 0 | 0 | 0 | 100 | - |
| red == 0 且 green == 0 | NA | NA | NA | 红绿校正信号均为0 |

因此不会出现 `Dead = 100, Live = 0, Red/Green = 0` 这种矛盾结果。

## 趋势检查

趋势检查只用于提示：

```text
不会修改原始数据、阈值或计算结果。
```

不会做以下操作：

- 不排序实验组来制造递增。
- 不删除异常值来制造递增。
- 不调换 AUR2 和 AUR4。
- 不动态调整阈值直到符合预期。
- 不修改 Dead % 结果。

对 AUR/PBMC 设计，趋势会分开描述：

```text
无 PBMC: CON -> AUR2 uM -> AUR4 uM
有 PBMC: CON+PBMC -> AUR(2uM)+PBMC -> AUR(4uM)+PBMC
```

不符合预期时只给黄色提示，不会判定分析失败。

## 输出文件

每次运行会在输出目录下生成：

```text
imagej_run_YYYYMMDD_HHMMSS/
```

主要文件：

| 文件 | 说明 |
|---|---|
| `ImageJ_fluorescence_quantification.ijm` | 本次运行生成的 ImageJ 宏 |
| `imagej_raw_measurements.csv` | ImageJ 原始 ROI 测量结果 |
| `roi_level_results.csv` | ROI 级计算结果 |
| `repeat_level_results.csv` | 生物学重复级均值 |
| `group_summary.csv` | 实验组 mean 和 sample SD |
| `replicate_summary.csv` | 兼容旧命名的组汇总 |
| `threshold_log.csv` | 每个 ROI 的红绿阈值和背景记录 |
| `qc_report.csv` | QC 警告 |
| `analysis_settings.json` | 本次分析参数 |
| `AUR_ImageJ_fluorescence_formula_results.xlsx` | 带公式的 Excel 工作簿 |
| `report.html` | HTML 报告 |
| `figures/` | SVG 图 |
| `imagej_masks/` | 红绿 mask 和 ROI overlay |
| `prism/` | GraphPad Prism 导入表 |

Prism 相关：

```text
prism/Prism_RedGreen_ratio_column.csv
prism/Prism_DeadLive_percent_grouped.csv
prism/Prism_group_summary.csv
prism/Prism_repeat_level_results.csv
```

`Prism_RedGreen_ratio_column.csv` 和 `Prism_DeadLive_percent_grouped.csv` 下方会附带 `三组重复平均值` 附表。

## ROI 级 CSV 关键字段

`roi_level_results.csv` 至少包含：

```text
Group
Biological_repeat
ROI_number
ROI_x
ROI_y
ROI_radius
ROI_area
Red_threshold
Green_threshold
Red_raw_integrated
Red_positive_pixel_count
Red_positive_intensity_sum
Red_background_mean
Red_corrected
Green_raw_integrated
Green_positive_pixel_count
Green_positive_intensity_sum
Green_background_mean
Green_corrected
Red_Green_ratio
Dead_percent
Live_percent
Red_saturated_percent
Green_saturated_percent
QC_warning
```

## 常见问题

### 页面打不开

确认终端里已经启动：

```bash
python3 run_app.py
```

如果 8765 端口被占用，程序会自动尝试后续端口，例如 8766。

### Fiji 找不到

在页面底部 `Fiji/ImageJ 路径` 中填写 Fiji 可执行文件路径，例如：

```text
/usr/local/bin/fiji
```

### Fiji 报 Java 或 headless 错误

先在终端检查：

```bash
/usr/local/bin/fiji --headless --help
```

如果 Fiji 是通过 `.app` 安装的，确认命令行可执行文件实际位置。

### Prism 表里为什么是 3 个重复而不是 9 个 ROI

因为统计规则是：

```text
3 个 ROI -> 先平均成 1 个生物学重复
3 个生物学重复 -> 再计算组 mean 和 SD
```

这是为了避免把技术 ROI 当作生物学重复。

## 开发和测试

安装测试依赖：

```bash
python3 -m pip install pytest
```

语法检查：

```bash
python3 -m compileall run_app.py src
```

运行测试：

```bash
python3 -m pytest -q
```

使用已有图片直接验证 Fiji 宏时，可以参考：

```bash
/usr/local/bin/fiji --headless -macro \
  outputs/imagej_run_YYYYMMDD_HHMMSS/ImageJ_fluorescence_quantification.ijm \
  outputs/imagej_run_YYYYMMDD_HHMMSS/extracted_images
```

不要额外传入 `--console`。

## 数据伦理和限制

本工具不会为了符合预期趋势而改动数据。用户仍需要人工检查：

- 原始图片是否曝光过度。
- ROI 是否覆盖目标区域。
- PBMC 混合样本是否引入背景/密度偏差。
- 阈值是否适合当前实验批次。
- QC 报告中的警告是否影响结论。

## License

当前仓库尚未声明开源许可证。社区使用、二次分发或商用前，请先由仓库维护者补充 LICENSE 文件。
