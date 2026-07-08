from __future__ import annotations

from pathlib import Path

from .models import ImageTriplet
from .ppt_extractor import natural_key


VALID_ORDER_ITEMS = {"red", "green", "merge"}


def parse_group_names(text: str | None) -> list[str]:
    if not text:
        return []
    names = []
    for line in text.splitlines():
        name = line.strip()
        if name:
            names.append(name.replace(",", "，"))
    return names


def parse_order(text: str | None) -> tuple[str, str, str]:
    if not text:
        return ("red", "green", "merge")
    order = tuple(part.strip() for part in text.split(","))
    if len(order) != 3 or set(order) != VALID_ORDER_ITEMS:
        raise ValueError("图片顺序必须包含 red、green、merge，且每项只出现一次。")
    return order  # type: ignore[return-value]


def parse_replicates_per_group(text: str | None) -> int:
    if text is None or not text.strip():
        return 3
    try:
        value = int(text)
    except ValueError as exc:
        raise ValueError("每组重复次数必须是整数，且不少于 3。") from exc
    if value < 3:
        raise ValueError("每组重复次数不能少于 3，否则无法计算可靠 SD。")
    return value


def build_triplets(
    images: list[Path],
    group_names: list[str] | None = None,
    order: tuple[str, str, str] = ("red", "green", "merge"),
    replicates_per_group: int = 1,
) -> list[ImageTriplet]:
    if len(order) != 3 or set(order) != VALID_ORDER_ITEMS:
        raise ValueError("图片顺序必须是 red、green、merge 的排列。")
    if replicates_per_group < 1:
        raise ValueError("每组重复次数必须至少为 1。")
    ordered = sorted(images, key=natural_key)
    if len(ordered) < 3:
        raise ValueError("图片数量不足三张，无法组成红/绿/合并图分组。")

    measurement_count = len(ordered) // 3
    names = group_names or []
    triplets: list[ImageTriplet] = []
    for i in range(measurement_count):
        chunk = ordered[i * 3 : i * 3 + 3]
        mapping = {label: chunk[index] for index, label in enumerate(order)}
        group = names[i] if i < len(names) else f"Group {i + 1}"
        triplets.append(ImageTriplet(group=group.replace(",", "，"), red=mapping["red"], green=mapping["green"], merge=mapping["merge"]))
    return triplets
