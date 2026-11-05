def stream_response() -> list[bytes]:
    """Return raw PCM chunks without a WAV header."""
    return [b"\x00\x00"]
