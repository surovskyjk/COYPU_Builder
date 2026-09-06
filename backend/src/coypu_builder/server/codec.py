"""Binary envelope framing: `[u32 LE header_len][UTF-8 JSON header][blob 0][blob 1]…` (ADR 0003)."""

from __future__ import annotations

import struct

import msgspec
import numpy as np

from coypu_builder.protocol.messages import BlobRef, Envelope

_HEADER_LEN = struct.Struct("<I")
_encoder = msgspec.json.Encoder()
_decoder = msgspec.json.Decoder(Envelope)


def encode_frame(envelope: Envelope, tail: bytes = b"") -> bytes:
    header = _encoder.encode(envelope)
    return _HEADER_LEN.pack(len(header)) + header + tail


def decode_frame(frame: bytes) -> tuple[Envelope, bytes]:
    if len(frame) < _HEADER_LEN.size:
        raise ValueError("frame shorter than the header-length prefix")
    (header_len,) = _HEADER_LEN.unpack_from(frame, 0)
    start = _HEADER_LEN.size
    envelope = _decoder.decode(frame[start : start + header_len])
    return envelope, frame[start + header_len :]


def pack_blobs(named_arrays: dict[str, np.ndarray]) -> tuple[tuple[BlobRef, ...], bytes]:
    """Concatenate arrays into one tail buffer, little-endian C-order, and describe each as a `BlobRef`."""
    refs: list[BlobRef] = []
    chunks: list[bytes] = []
    offset = 0
    for name, array in named_arrays.items():
        array = np.ascontiguousarray(array)
        data = array.tobytes()
        refs.append(
            BlobRef(
                name=name, dtype=array.dtype.str, shape=tuple(array.shape), offset=offset, length=len(data)
            )
        )
        chunks.append(data)
        offset += len(data)
    return tuple(refs), b"".join(chunks)


def unpack_blob(tail: bytes, ref: BlobRef) -> np.ndarray:
    raw = tail[ref.offset : ref.offset + ref.length]
    return np.frombuffer(raw, dtype=np.dtype(ref.dtype)).reshape(ref.shape)
