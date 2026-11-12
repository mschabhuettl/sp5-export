"""Farbkonvertierung für Schichtplaner5-Farben (gespeichert als Windows-BGR-Integer)."""


def bgr_to_hex(bgr: int) -> str:
    """Convert Windows BGR integer to HTML hex color string."""
    if not isinstance(bgr, int) or bgr < 0:
        return "#FFFFFF"
    b = (bgr >> 16) & 0xFF
    g = (bgr >> 8) & 0xFF
    r = bgr & 0xFF
    return f"#{r:02X}{g:02X}{b:02X}"


def bgr_to_rgb(bgr: int) -> tuple:
    """Convert Windows BGR integer to (R, G, B) tuple."""
    b = (bgr >> 16) & 0xFF
    g = (bgr >> 8) & 0xFF
    r = bgr & 0xFF
    return (r, g, b)


def is_light_color(bgr: int) -> bool:
    """Liefert True bei heller Farbe (dunklen Text darauf verwenden)."""
    r, g, b = bgr_to_rgb(bgr)
    luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return luminance > 0.5
