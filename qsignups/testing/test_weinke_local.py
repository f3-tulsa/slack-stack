"""
Local tests for weinke PNG generation (no DB / no real S3).

Run from repo root:
  cd qsignups/qsignups && PYTHONPATH=. python3 ../testing/test_weinke_local.py
"""
from __future__ import annotations

import io
import os
import sys
from datetime import date
from unittest.mock import MagicMock, patch

_PKG = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "qsignups"))
if _PKG not in sys.path:
    sys.path.insert(0, _PKG)


def test_cell_color_logic() -> None:
    import weinke as w

    assert w.cell_background_color("") == w.BG_DEFAULT
    assert w.cell_background_color("   ") == w.BG_DEFAULT
    assert w.cell_background_color("Roller\n0530") == w.BG_DEFAULT
    assert w.cell_background_color("OPEN!\n0530") == w.BG_OPEN
    assert w.cell_background_color("OPEN\n0530") == w.BG_OPEN
    assert w.cell_background_color("The Forge thing\nBC 0530") == w.BG_FORGE
    assert w.cell_background_color("Someone\nVQ\n0530") == w.BG_SPECIAL_BLUE
    assert w.cell_background_color("Someone\nAO Launch\n0530") == w.BG_SPECIAL_BLUE
    assert w.cell_background_color("X\n24 Hr Beatdown\n0530") == w.BG_SPECIAL_BLUE


def test_grid_pivot() -> None:
    import weinke as w

    ao = MagicMock()
    ao.ao_channel_id = "C1"
    ao.ao_display_name = "My AO"
    ao.ao_location_subtitle = "Park"

    m1 = MagicMock()
    m1.ao_channel_id = "C1"
    m1.event_date = date(2025, 6, 4)  # Wed in week Jun 2–8, 2025
    m1.event_time = "0530"
    m1.q_pax_name = None
    m1.event_special = None

    with patch.object(w.DbManager, "find_records", return_value=[m1]):
        headers, rows = w.build_week_grid("T1", date(2025, 6, 2), date(2025, 6, 8), {"C1": ao})

    assert headers[0] == "AO\nLocation"
    assert len(headers) == 8  # AO + 7 days
    assert len(rows) == 1
    assert "My AO" in rows[0][0]
    # Jun 4 is 3rd calendar day of range (Mon Tue Wed) -> column index 3
    assert "OPEN" in rows[0][3]


def test_render_produces_valid_png() -> None:
    import weinke as w

    png = w.render_table_png(["A", "B"], [["x", "y"]])
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_dynamic_height() -> None:
    import weinke as w
    from PIL import Image

    headers = ["AO\nLocation"] + [f"D{i}" for i in range(7)]
    rows5 = [["ao"]] + [[""] * 7 for _ in range(5)]
    rows20 = [["ao"]] + [[""] * 7 for _ in range(20)]
    # Fix: rows should be list of rows each length len(headers)
    rows5 = [["ao" + str(i)] + [""] * 7 for i in range(5)]
    rows20 = [["ao" + str(i)] + [""] * 7 for i in range(20)]

    b5 = w.render_table_png(headers, rows5)
    b20 = w.render_table_png(headers, rows20)
    h5 = Image.open(io.BytesIO(b5)).height
    h20 = Image.open(io.BytesIO(b20)).height
    assert h20 > h5, f"expected taller image for more rows: {h5} vs {h20}"


def test_upload_to_s3_calls_boto3() -> None:
    import weinke as w

    with patch.object(w.boto3, "client") as m:
        inst = MagicMock()
        m.return_value = inst
        url = w.upload_to_s3(b"fakepng", "my-bucket", "weinkes/T1_current_week_weinke.png")
        inst.put_object.assert_called_once()
        kw = inst.put_object.call_args.kwargs
        assert kw["Bucket"] == "my-bucket"
        assert kw["Key"] == "weinkes/T1_current_week_weinke.png"
        assert kw["ContentType"] == "image/png"
        assert kw["Body"] == b"fakepng"
        assert url.startswith(
            "https://my-bucket.s3.amazonaws.com/weinkes/T1_current_week_weinke.png?v="
        )
        assert url.split("?v=", 1)[1].isdigit()


def test_generate_and_store_updates_db() -> None:
    from database.orm import Region

    import weinke as w

    ao = MagicMock()
    ao.ao_channel_id = "C1"
    ao.ao_display_name = "AO1"
    ao.ao_location_subtitle = ""

    os.environ["IMAGE_S3_BUCKET"] = "test-bucket"

    def find_side_effect(model, filters):
        if getattr(model, "__tablename__", None) == "qsignups_aos":
            return [ao]
        return []

    with patch.object(w.DbManager, "find_records", side_effect=find_side_effect):
        with patch.object(w, "upload_to_s3", return_value="https://test-bucket.s3.amazonaws.com/weinkes/x.png"):
            with patch.object(w.DbManager, "update_record") as up:
                w.generate_and_store_weinke("TTEAM", None)

    up.assert_called_once()
    args, _kwargs = up.call_args
    assert args[0] is w.Region
    assert args[1] == "TTEAM"
    fields = args[2]
    assert Region.current_week_weinke in fields
    assert Region.next_week_weinke in fields
    assert fields[Region.current_week_weinke].startswith("https://")
    assert fields[Region.next_week_weinke].startswith("https://")


def main() -> None:
    os.environ.setdefault("DB_ENCRYPTION_KEY", "test-encryption-key-min-16")

    test_cell_color_logic()
    print("cell_background_color OK")
    test_grid_pivot()
    print("build_week_grid OK")
    test_render_produces_valid_png()
    print("render_table_png OK")
    test_dynamic_height()
    print("dynamic height OK")
    test_upload_to_s3_calls_boto3()
    print("upload_to_s3 OK")
    test_generate_and_store_updates_db()
    print("generate_and_store_weinke OK")


if __name__ == "__main__":
    main()
