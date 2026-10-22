"""The binary tail of a frame (ADR 0003): how each blob is described in its header."""

from __future__ import annotations

import numpy as np

from coypu_builder.server.codec import pack_blobs, unpack_blob


def test_one_byte_blobs_are_described_as_little_endian_u1_and_round_trip():
    refs, tail = pack_blobs({"a": np.zeros(3, np.uint8)})
    (ref,) = refs
    assert ref.dtype == "<u1"
    values = np.arange(6, dtype=np.uint8).reshape(2, 3)
    refs, tail = pack_blobs({"b": values})
    assert refs[0].dtype == "<u1" and refs[0].shape == (2, 3)
    assert np.array_equal(unpack_blob(tail, refs[0]), values)


def test_multi_byte_blobs_keep_their_byte_order_prefix():
    refs, tail = pack_blobs({"f": np.arange(3, dtype="<f4"), "i": np.arange(2, dtype="<i4")})
    assert [r.dtype for r in refs] == ["<f4", "<i4"]
    assert np.array_equal(unpack_blob(tail, refs[0]), np.arange(3, dtype="<f4"))
