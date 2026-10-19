"""Small text charts for the TUI. Each widget redraws to fit its width; colours are theme variables
so light and dark themes both work. Detail lives in the dashboard and reports, not here."""

from __future__ import annotations

from textual.content import Content
from textual.widget import Widget

BLOCKS = " ▁▂▃▄▅▆▇█"
SPARK = "▁▂▃▄▅▆▇█"


def spark(values: list[float], width: int | None = None) -> str:
    """A one-line sparkline; resamples to `width` characters when given."""
    vals = [v or 0.0 for v in values]
    if width and len(vals) > width:
        step = len(vals) / width
        vals = [max(vals[int(i * step):max(int((i + 1) * step), int(i * step) + 1)]) for i in range(width)]
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    if hi == lo:
        return SPARK[len(SPARK) // 2 if hi else 0] * len(vals)
    return "".join(SPARK[round((v - lo) / (hi - lo) * (len(SPARK) - 1))] for v in vals)


def compact(v: float) -> str:
    a = abs(v)
    s = f"${a / 1000:.1f}K".replace(".0K", "K") if a >= 1000 else f"${a:,.0f}"
    return ("-" if v < 0 else "") + s


def _esc(s: str) -> str:
    return s.replace("[", "\\[")


class ColumnChart(Widget):
    """Vertical bars, one per item: [(label, value, style)]. Shows the max on the axis and labels
    every column whose label fits."""

    DEFAULT_CSS = "ColumnChart { height: 9; }"

    def __init__(self, *, id: str | None = None, rows: int = 6) -> None:
        super().__init__(id=id)
        self.items: list[tuple[str, float, str]] = []
        self.rows = rows
        self.private = False
        self.note = ""

    def set_data(self, items: list[tuple[str, float, str]], note: str = "", private: bool = False) -> None:
        self.items, self.note, self.private = items, note, private
        self.refresh()

    def render(self) -> Content:
        if not self.items:
            return Content.from_markup("[$text-muted]No data yet[/]")
        width = self.size.width or 60
        axis = 0 if self.private else 7
        n = len(self.items)
        col = max(1, min(5, (width - axis) // n - 1))
        top = max(v for _, v, _ in self.items) or 1
        levels = self.rows * 8
        heights = [round(max(v, 0) / top * levels) for _, v, _ in self.items]
        lines = []
        for r in range(self.rows, 0, -1):
            label = (compact(top).rjust(axis - 1) + " ") if (r == self.rows and axis) else " " * axis
            cells = []
            for (_, _, style), hgt in zip(self.items, heights):
                fill = min(max(hgt - (r - 1) * 8, 0), 8)
                block = BLOCKS[fill] * col
                cells.append(f"[{style}]{block}[/]" if fill else block)
            lines.append(f"[$text-muted]{label}[/]" + " ".join(cells))
        labels = []
        for lab, _, _ in self.items:
            labels.append(lab[:col].ljust(col))
        lines.append(" " * axis + "[$text-muted]" + _esc(" ".join(labels)) + "[/]")
        if self.note:
            lines.append(f"[$text-muted]{_esc(self.note)}[/]")
        return Content.from_markup("\n".join(lines))


class CompareBars(Widget):
    """Horizontal bars with a typical-value tick: [(name, value, typical)]. Over typical shows in
    the warning colour; the tick marks typical."""

    DEFAULT_CSS = "CompareBars { height: auto; }"

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.items: list[tuple[str, float, float | None]] = []
        self.private = False
        self.note = ""

    def set_data(self, items: list[tuple[str, float, float | None]], note: str = "", private: bool = False) -> None:
        self.items, self.note, self.private = items, note, private
        self.refresh(layout=True)

    def get_content_height(self, container, viewport, width: int) -> int:
        return max(1, len(self.items)) + (1 if self.note else 0)

    def render(self) -> Content:
        if not self.items:
            return Content.from_markup("[$text-muted]No spending yet this month[/]")
        width = self.size.width or 60
        name_w = min(14, max(len(n) for n, _, _ in self.items))
        value_w = 0 if self.private else 15
        bar_w = max(8, width - name_w - value_w - 3)
        top = max(max(v, t or 0) for _, v, t in self.items) or 1
        lines = []
        for name, v, typical in self.items:
            cells = round(v / top * bar_w * 8)
            full, part = divmod(cells, 8)
            bar = list("█" * full + (BLOCKS[part] if part else ""))
            bar += [" "] * (bar_w - len(bar))
            tick = None if typical is None else min(bar_w - 1, round(typical / top * bar_w))
            over = typical is not None and v > typical * 1.1
            style = "$warning" if over else "$primary"
            out = []
            for i, ch in enumerate(bar[:bar_w]):
                if i == tick:
                    out.append(f"[$foreground]{'┃' if ch == ' ' else '╋'}[/]")
                else:
                    out.append(f"[{style}]{ch}[/]" if ch != " " else " ")
            nm = _esc(name[:name_w].ljust(name_w))
            vals = "" if self.private else (
                f" {compact(v):>6}" + (f" [$text-muted]/ {compact(typical):>5}[/]" if typical is not None else ""))
            lines.append(f"{nm} {''.join(out)}{vals}")
        if self.note:
            lines.append(f"[$text-muted]{_esc(self.note)}[/]")
        return Content.from_markup("\n".join(lines))
