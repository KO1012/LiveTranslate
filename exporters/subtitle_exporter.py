from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SubtitleExportItem:
    index: int
    timestamp: str
    original: str = ""
    translation: str = ""
    start_seconds: float | None = None
    end_seconds: float | None = None


def export_subtitles(items: list[SubtitleExportItem], mode: str, fmt: str) -> str:
    fmt = fmt.lower()
    if fmt == "srt":
        return _export_srt(items, mode)
    if fmt == "md":
        return _export_markdown(items, mode)
    return _export_txt(items, mode)


def subtitle_rows_to_export_items(rows: list[dict]) -> list[SubtitleExportItem]:
    base_start = None
    for row in rows:
        if row.get("start_time") is not None:
            base_start = float(row["start_time"])
            break
    items = []
    for idx, row in enumerate(rows, 1):
        start = None
        end = None
        if base_start is not None and row.get("start_time") is not None:
            start = float(row["start_time"]) - base_start
        if base_start is not None and row.get("end_time") is not None:
            end = float(row["end_time"]) - base_start
        items.append(
            SubtitleExportItem(
                index=idx,
                timestamp=row.get("created_at") or "",
                original=row.get("original_text") or "",
                translation=row.get("translated_text") or "",
                start_seconds=start,
                end_seconds=end,
            )
        )
    return items


def _export_txt(items: list[SubtitleExportItem], mode: str) -> str:
    lines = []
    for item in items:
        if mode == "original":
            if item.original:
                lines.append(f"[{item.timestamp}] {item.original}")
        elif mode == "translation":
            if item.translation:
                lines.append(f"[{item.timestamp}] {item.translation}")
        else:
            if item.original:
                lines.append(f"[{item.timestamp}] {item.original}")
            if item.translation:
                lines.append(f"  -> {item.translation}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _export_srt(items: list[SubtitleExportItem], mode: str) -> str:
    blocks = []
    for out_idx, item in enumerate(items, 1):
        text = _item_text(item, mode)
        if not text:
            continue
        start = item.start_seconds
        if start is None:
            start = max(0.0, item.index - 1) * 4.0
        end = item.end_seconds
        if end is None or end <= start:
            end = start + 4.0
        blocks.append(
            f"{out_idx}\n{_format_srt_time(start)} --> {_format_srt_time(end)}\n{text}"
        )
    return "\n\n".join(blocks).rstrip() + "\n"


def _export_markdown(items: list[SubtitleExportItem], mode: str) -> str:
    lines = ["# 字幕记录", ""]
    for item in items:
        if not _item_text(item, mode):
            continue
        start = _format_md_time(item.start_seconds, item.timestamp)
        end = _format_md_time(item.end_seconds, None)
        title = f"## {start}" if not end else f"## {start} - {end}"
        lines.extend([title, ""])
        if mode in ("original", "both") and item.original:
            lines.extend(["原文：", item.original, ""])
        if mode in ("translation", "both") and item.translation:
            lines.extend(["译文：", item.translation, ""])
    return "\n".join(lines).rstrip() + "\n"


def _item_text(item: SubtitleExportItem, mode: str) -> str:
    if mode == "original":
        return item.original.strip()
    if mode == "translation":
        return item.translation.strip()
    parts = [p for p in (item.original.strip(), item.translation.strip()) if p]
    return "\n".join(parts)


def _format_srt_time(seconds: float) -> str:
    millis = int(round(max(0.0, seconds) * 1000))
    ms = millis % 1000
    total_seconds = millis // 1000
    sec = total_seconds % 60
    total_minutes = total_seconds // 60
    minute = total_minutes % 60
    hour = total_minutes // 60
    return f"{hour:02d}:{minute:02d}:{sec:02d},{ms:03d}"


def _format_md_time(seconds: float | None, fallback: str | None) -> str:
    if seconds is None:
        return fallback or ""
    total_seconds = int(max(0.0, seconds))
    sec = total_seconds % 60
    total_minutes = total_seconds // 60
    minute = total_minutes % 60
    hour = total_minutes // 60
    return f"{hour:02d}:{minute:02d}:{sec:02d}"
