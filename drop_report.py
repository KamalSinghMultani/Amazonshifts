"""Read the detection log back and work out when shifts actually drop.

Rather than guess at hot windows, measure them. Run the watcher in dry run for
a few days, then `python watcher.py --drop-report` turns your own data into a
`hot_windows` block you can paste into config.yaml.

Pure functions over a list of {"ts": float} dicts — no browser, no clock
assumptions beyond "report in the user's local time", which is the same clock
hot_windows are interpreted in.
"""

from __future__ import annotations

from datetime import datetime

# An hour is "busy" if it carries at least this share of the peak hour's
# detections. Low enough to catch a genuine second batch, high enough that one
# stray overnight posting does not earn a window of its own.
BUSY_SHARE = 0.25
MIN_DETECTIONS = 2


def hourly_counts(entries: list[dict]) -> dict[int, int]:
    """Detections per local-time hour. Hours with none are omitted."""
    counts: dict[int, int] = {}
    for entry in entries:
        try:
            hour = datetime.fromtimestamp(float(entry["ts"])).hour
        except (KeyError, TypeError, ValueError, OSError):
            continue
        counts[hour] = counts.get(hour, 0) + 1
    return counts


def busy_hours(counts: dict[int, int]) -> list[int]:
    if not counts:
        return []
    peak = max(counts.values())
    threshold = max(MIN_DETECTIONS, peak * BUSY_SHARE)
    return sorted(hour for hour, count in counts.items() if count >= threshold)


def suggest_windows(counts: dict[int, int]) -> list[str]:
    """Busy hours merged into as few windows as possible.

    Adjacent hours become one window (6 and 7 -> "06:00-08:00") because two
    back-to-back windows behave identically but read worse. Windows end at the
    top of the following hour, so a 06:xx drop is still covered.
    """
    hours = busy_hours(counts)
    if not hours:
        return []

    windows: list[str] = []
    start = previous = hours[0]
    for hour in hours[1:]:
        if hour == previous + 1:
            previous = hour
            continue
        windows.append(f"{start:02d}:00-{(previous + 1) % 24:02d}:00")
        start = previous = hour
    windows.append(f"{start:02d}:00-{(previous + 1) % 24:02d}:00")
    return windows


def render(entries: list[dict], bar_width: int = 40) -> str:
    """The human-facing report."""
    if not entries:
        return (
            "No detections logged yet.\n\n"
            "Run the watcher in dry run across a day or two first — this report "
            "reads state/detections.jsonl, which it writes as it goes."
        )

    stamps = sorted(float(e["ts"]) for e in entries if "ts" in e)
    first = datetime.fromtimestamp(stamps[0]).strftime("%Y-%m-%d %H:%M")
    last = datetime.fromtimestamp(stamps[-1]).strftime("%Y-%m-%d %H:%M")

    counts = hourly_counts(entries)
    lines = [
        f"{len(entries)} detection(s) from {first} to {last}",
        "",
        "Detections by hour (your local time):",
    ]

    peak = max(counts.values())
    for hour in sorted(counts):
        count = counts[hour]
        bar = "#" * max(1, round(bar_width * count / peak))
        lines.append(f"  {hour:02d}:00 {bar:<{bar_width}} {count}")

    windows = suggest_windows(counts)
    lines += ["", "Suggested config.yaml:", "", "polling:", "  hot_windows:"]
    if windows:
        lines += [f'    - "{w}"' for w in windows]
    else:
        lines.append("    []   # not enough data yet to call it")
    return "\n".join(lines)
