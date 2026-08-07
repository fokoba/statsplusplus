"""Display formatting utilities.

Single source of truth for all human-readable value formatting.
Used by both web templates (via Jinja filters) and CLI output.
"""

from __future__ import annotations


def fmt_money(val: int | float | str | None) -> str:
    """Format a dollar amount: $1.2M for millions, $150K for thousands.

    Args:
        val: Dollar amount (int/float), or None/string for passthrough.

    Returns:
        Formatted string like "$1.2M", "$150K", "$825", or "—" for None.
    """
    if val is None:
        return "—"
    if isinstance(val, str):
        return val
    if abs(val) >= 1_000_000:
        return f"${val / 1e6:.1f}M"
    if abs(val) >= 1_000:
        return f"${val / 1e3:.0f}K"
    return f"${val:,.0f}"


def fmt_ip(ip: float | int | str | None) -> str:
    """Format true decimal IP (e.g., 33.333) as baseball notation (33.1).

    Baseball convention: fractional innings are displayed as .0, .1, .2
    representing 0, 1, or 2 outs in the partial inning.

    Args:
        ip: Innings pitched as decimal (outs / 3), or None/string for passthrough.

    Returns:
        Baseball-formatted IP string like "33.1", or "-" for None.
    """
    if ip is None or isinstance(ip, str):
        return ip or "-"
    full = int(ip)
    frac = round((ip - full) * 3)
    return f"{full}.{frac}" if frac else f"{full}.0"


def short_name(name: str) -> str:
    """Abbreviate a full name to first initial + last name.

    Handles suffixes (Jr., Sr., II, III, IV) by preserving them.

    Examples:
        "Mike Trout" -> "M. Trout"
        "Fernando Tatis Jr." -> "F. Tatis Jr."
        "Ken Griffey Jr" -> "K. Griffey Jr"
    """
    parts = name.split()
    if len(parts) < 2:
        return name
    _SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv"}
    suffix = ""
    if parts[-1].lower().rstrip(".") in _SUFFIXES:
        suffix = " " + parts[-1]
        parts = parts[:-1]
    if len(parts) < 2:
        return name
    return f"{parts[0][:1]}. {parts[-1]}{suffix}"


def height_str(cm: int | None) -> str | None:
    """Convert height in centimeters to feet'inches" format.

    Args:
        cm: Height in centimeters, or None.

    Returns:
        String like "6'2\"", or None if input is None/0.
    """
    if not cm:
        return None
    feet = int(cm / 30.48)
    inches = round((cm % 30.48) / 2.54)
    return f"{feet}'{inches}\""


def fmt_pct(val: float | None, decimals: int = 1) -> str:
    """Format a decimal as percentage string.

    Args:
        val: Value like 0.325, or None.
        decimals: Number of decimal places.

    Returns:
        String like "32.5%" or "—" for None.
    """
    if val is None:
        return "—"
    return f"{val * 100:.{decimals}f}%"


def fmt_avg(val: float | None) -> str:
    """Format a batting average (.325 display format).

    Baseball convention: no leading zero for averages < 1.0.

    Args:
        val: Batting average as float (e.g., 0.325), or None.

    Returns:
        String like ".325" or "—" for None.
    """
    if val is None:
        return "—"
    formatted = f"{val:.3f}"
    if formatted.startswith("0."):
        return formatted[1:]  # Strip leading zero
    return formatted


def fmt_table(headers: list[str], values: list[str]) -> str:
    """Format a single-row markdown table.

    Args:
        headers: Column header strings.
        values: Column value strings.

    Returns:
        Formatted markdown table with header, separator, and value rows.
    """
    col_w = [max(len(h), len(v)) for h, v in zip(headers, values)]
    h_row = "| " + " | ".join(h.ljust(w) for h, w in zip(headers, col_w)) + " |"
    s_row = "| " + " | ".join("-" * w for w in col_w) + " |"
    v_row = "| " + " | ".join(v.ljust(w) for v, w in zip(values, col_w)) + " |"
    return "\n".join([h_row, s_row, v_row])
