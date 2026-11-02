import io
from datetime import UTC, datetime

import fitdecode

from fitwriter import FitWriter, fit_crc, ts
from wristpull.fit.decode import decode_fit


def test_crc_known_vector():
    # CRC of an empty buffer is 0; of b".FIT" header-like data must be stable
    assert fit_crc(b"") == 0
    assert fit_crc(b"123456789") == 0xBB3D  # CRC-16/ARC check value


def test_writer_roundtrip_is_valid_fit():
    w = FitWriter()
    created = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    w.message(0, [(0, "enum", 4), (1, "uint16", 1), (4, "uint32", ts(created)), (8, "string", "fenix")])
    w.message(20, [(253, "uint32", ts(created)), (3, "uint8", 123), (65, "uint32", [1, 2, 3])])
    w.message(20, [(253, "uint32", ts(created) + 1), (3, "uint8", 124), (65, "uint32", [4, 5, 6])])
    data = w.finish()
    # strict CRC check must pass
    with fitdecode.FitReader(io.BytesIO(data), check_crc=fitdecode.CrcCheck.RAISE) as reader:
        frames = [f for f in reader if isinstance(f, fitdecode.FitDataMessage)]
    assert [f.global_mesg_num for f in frames] == [0, 20, 20]

    fit = decode_fit(data)
    assert fit.file_type == "activity"
    assert fit.time_created == created
    assert fit.product_name == "fenix"
    recs = fit.of("record")
    assert [r.get(3) for r in recs] == [123, 124]
    assert recs[1].timestamp() == created.replace(second=6)
    assert fit.warnings == []


def test_big_endian_writer_roundtrip():
    w = FitWriter(big_endian=True)
    created = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    w.message(0, [(0, "enum", 5), (1, "uint16", 1), (3, "uint32z", 123456789), (4, "uint32", ts(created)), (8, "string", "x")])
    w.message(26, [(4, "enum", 1), (6, "uint16", 3), (8, "string", "Big")])
    data = w.finish()
    assert data[14 + 2] == 1  # architecture byte of the first definition
    fit = decode_fit(data)
    assert fit.file_type == "workout" and fit.serial_number == 123456789 and fit.time_created == created
    assert fit.of("workout")[0].get("num_valid_steps") == 3 and fit.of("workout")[0].get("wkt_name") == "Big"
