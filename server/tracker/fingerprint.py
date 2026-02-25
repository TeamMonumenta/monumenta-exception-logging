import hashlib
import re

_UUID_RE = re.compile(
    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
    re.IGNORECASE,
)
_IP_RE = re.compile(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b')
_LONG_NUM_RE = re.compile(r'\b\d{4,}\b')
_QUOTED_SINGLE_RE = re.compile(r"'[^']{1,64}'")
_QUOTED_DOUBLE_RE = re.compile(r'"[^"]{1,64}"')
_BRACKET_DATA_RE = re.compile(r'\[[^\[\]]{0,256}\]')


def normalize_message(message: str) -> str:
    s = _UUID_RE.sub('<uuid>', message)
    s = _IP_RE.sub('<ip>', s)
    s = _LONG_NUM_RE.sub('<N>', s)
    s = _QUOTED_SINGLE_RE.sub('<str>', s)
    s = _QUOTED_DOUBLE_RE.sub('<str>', s)
    s = _BRACKET_DATA_RE.sub('<data>', s)
    return s


def extract_app_frames(
    frames: list[dict], app_packages: list[str], count: int
) -> list[dict]:
    result = [
        f for f in frames
        if any(f.get('class_name', '').startswith(pkg) for pkg in app_packages)
    ][:count]
    if not result:
        result = frames[:count]
    return result


def compute_fingerprint(
    exception_class: str, normalized_message: str, top_frames: list[dict]
) -> str:
    frame_str = '|'.join(
        f"{f['class_name']}.{f['method']}" for f in top_frames
    )
    components = [exception_class, normalized_message, frame_str]
    return hashlib.sha256('|'.join(components).encode()).hexdigest()
