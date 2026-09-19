"""The one helper `pulse.py` needs from artel-agent-server's envelope module."""


def center_of(x: float, y: float, w: float, h: float) -> tuple[int, int]:
    """조준점을 모서리와 크기에서 뽑는다. 좌표는 자른다 — 픽셀 아래는 다르게 그려지지 않는다."""
    return int(x) + int(w) // 2, int(y) + int(h) // 2
