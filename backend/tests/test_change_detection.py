import asyncio
import base64
import io
import json
import re
import sys
import unittest
import zlib
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from fastapi import UploadFile
from PIL import Image
from starlette.datastructures import Headers

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from app.main import Analysis, change, report


def make_upload(name, pixels):
    image = Image.fromarray(pixels.astype(np.uint8), mode="RGB")
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    stream.seek(0)
    return UploadFile(file=stream, filename=name, headers=Headers({"content-type": "image/png"}))


class ChangeDetectionTests(unittest.TestCase):
    def test_change_result_keeps_both_sources_and_report_download(self):
        before = np.full((48, 64, 3), 90, dtype=np.uint8)
        after = before.copy()
        after[12:36, 20:50] = (220, 210, 180)

        class WriteSession:
            def add(self, row):
                if isinstance(row, Analysis):
                    row.created = datetime(2026, 1, 1)
                    self.analysis = row

            def commit(self):
                pass

        writer = WriteSession()
        response = change(
            earlier=make_upload("before.png", before),
            later=make_upload("after.png", after),
            name="Test comparison",
            location="",
            lat="",
            lon="",
            source="SAMPLE DATA",
            before_date="2026-01-01",
            after_date="2026-02-01",
            u=SimpleNamespace(id=1),
            s=writer,
        )
        data = response["result"]
        self.assertEqual(data["source"], "SAMPLE DATA")
        self.assertEqual(data["before_date"], "2026-01-01")
        self.assertEqual(data["after_date"], "2026-02-01")
        self.assertEqual(data["before_filename"], "before.png")
        self.assertEqual(data["after_filename"], "after.png")
        self.assertIn("before_image", data)
        self.assertIn("after_image", data)
        self.assertGreater(data["changed_percent"], 0)

        class Query:
            def filter_by(self, **kwargs):
                return self

            def first(self):
                return writer.analysis

        class ReadSession:
            def query(self, model):
                return Query()

        pdf_response = report(response["id"], SimpleNamespace(id=1), ReadSession())

        async def collect_body():
            return b"".join([chunk async for chunk in pdf_response.body_iterator])

        pdf = asyncio.run(collect_body())
        self.assertTrue(pdf.startswith(b"%PDF"))
        decoded = []
        for stream in re.findall(rb"stream\r?\n(.*?)~>endstream", pdf, flags=re.S):
            try:
                decoded.append(zlib.decompress(base64.a85decode(stream, adobe=False)))
            except (ValueError, zlib.error):
                continue
        pdf_text = b"\n".join(decoded)
        self.assertIn(b"Change detection visual review", pdf_text)
        self.assertIn(b"Before image", pdf_text)
        self.assertIn(b"After image", pdf_text)
        self.assertIn(b"Changed Percent", pdf_text)


if __name__ == "__main__":
    unittest.main()
