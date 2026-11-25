import asyncio
import base64
import io
import json
import re
import sys
import unittest
import zlib
from pathlib import Path
from datetime import datetime
from types import SimpleNamespace

import numpy as np
from fastapi import UploadFile
from PIL import Image
from starlette.datastructures import Headers

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from app.main import Analysis, flood, flood_comparison, flood_water_mask, png_data, report


def make_upload(name, pixels):
    stream = io.BytesIO()
    Image.fromarray(pixels.astype(np.uint8), mode="RGB").save(stream, format="PNG")
    stream.seek(0)
    return UploadFile(file=stream, filename=name, headers=Headers({"content-type": "image/png"}))


class FloodDetectionTests(unittest.TestCase):
    def test_available_demo_scenes_detect_the_synthetic_river(self):
        for name in ("demo_scene.png", "demo_scene_after.png"):
            with self.subTest(image=name):
                rgb = np.asarray(Image.open(ROOT / "backend" / "sample_data" / name).convert("RGB"))
                mask = flood_water_mask(rgb)
                self.assertGreater(float(mask.mean()), 0.03)
                self.assertLess(float(mask.mean()), 0.10)

    def test_muddy_water_detected_without_marking_textured_farm(self):
        rng = np.random.default_rng(7)
        rgb = np.empty((160, 200, 3), dtype=np.uint8)
        rgb[:] = (83, 116, 61)
        rgb[15:65, 15:90] = (143, 139, 88)  # dry tan agricultural parcel
        noise = rng.integers(-30, 31, size=(80, 100, 1))
        rgb[70:150, 95:195] = np.clip(np.array([126, 96, 67]) + noise, 0, 255).astype(np.uint8)
        muddy_pool = np.clip(np.array([110, 78, 56]) + rng.normal(0, 2, size=(80, 80, 3)), 0, 255).astype(np.uint8)
        rgb[70:150, 10:90] = muddy_pool

        mask = flood_water_mask(rgb)
        r, g, b = [muddy_pool[:, :, i].astype(float) for i in range(3)]
        old_blue_only = (b > r * 1.08) & (b > g * 0.95) & ((b - r) > 12)
        self.assertLess(float(old_blue_only.mean()), 0.01)
        self.assertGreater(float(mask[70:150, 10:90].mean()), 0.95)
        self.assertLess(float(mask[15:65, 15:90].mean()), 0.01)
        self.assertLess(float(mask[70:150, 95:195].mean()), 0.01)

    def test_comparison_detects_new_muddy_water_but_excludes_permanent_water(self):
        rng = np.random.default_rng(12)
        before = np.zeros((160, 200, 3), dtype=np.uint8)
        before[:] = (83, 116, 61)
        before[20:140, 110:190] = (143, 139, 88)
        before[40:120, 20:85] = (43, 95, 135)
        after = before.copy()
        after[60:125, 95:185] = np.clip(
            np.array([110, 78, 56]) + rng.normal(0, 2, (65, 90, 3)), 0, 255
        ).astype(np.uint8)

        result = flood_comparison(before, after)
        self.assertTrue(result["comparison_valid"], result["alignment_warning"])
        self.assertGreater(result["new_water_pixels"], 0)
        self.assertGreater(result["new_water_percent"], 0)
        self.assertIn("cannot be independently confirmed", result["alignment_warning"])

        demo_before = np.asarray(Image.open(ROOT / "backend" / "sample_data" / "demo_scene.png").convert("RGB"))
        demo_after = np.asarray(Image.open(ROOT / "backend" / "sample_data" / "demo_scene_after.png").convert("RGB"))
        demo_result = flood_comparison(demo_before, demo_after)
        self.assertTrue(demo_result["comparison_valid"], demo_result["alignment_warning"])
        self.assertEqual(demo_result["new_water_percent"], 0)

    def test_misaligned_images_do_not_get_a_change_percentage(self):
        import cv2

        before = np.asarray(Image.open(ROOT / "backend" / "sample_data" / "demo_scene.png").convert("RGB"))
        after = cv2.warpAffine(before, np.float32([[1, 0, 22], [0, 1, 14]]), (before.shape[1], before.shape[0]))
        result = flood_comparison(before, after)
        self.assertFalse(result["comparison_valid"])
        self.assertIsNone(result["new_water_percent"])
        self.assertIsNone(result["new_water_pixels"])
        self.assertIn("alignment", result["alignment_warning"].lower())

    def test_flood_api_keeps_single_image_mode_and_adds_pair_mode(self):
        class WriteSession:
            def add(self, row):
                if isinstance(row, Analysis):
                    row.created = datetime(2026, 1, 1)

            def commit(self):
                pass

        user = SimpleNamespace(id=1)
        rgb = np.full((64, 64, 3), (83, 116, 61), dtype=np.uint8)
        single = flood(file=make_upload("single.png", rgb), before_file=None, after_file=None,
                       name="Single mode", location="", lat="", lon="", source="USER UPLOAD",
                       image_date="", before_date="", after_date="", u=user, s=WriteSession())
        self.assertEqual(single["result"]["analysis_mode"], "single")
        self.assertIn("mask_image", single["result"])

        paired = flood(
            before_file=make_upload("before.png", rgb),
            after_file=make_upload("after.png", rgb),
            source="SAMPLE DATA",
            name="Paired mode",
            location="",
            lat="",
            lon="",
            image_date="",
            before_date="2026-01-01",
            after_date="2026-01-02",
            u=user,
            s=WriteSession(),
        )
        self.assertEqual(paired["result"]["analysis_mode"], "comparison")
        self.assertEqual(paired["result"]["source"], "SAMPLE DATA")
        self.assertEqual(paired["result"]["before_date"], "2026-01-01")
        self.assertIn("change_mask_image", paired["result"])

    def test_pdf_report_contains_measured_flood_statistics_and_visual_review(self):
        rgb = np.zeros((32, 32, 3), dtype=np.uint8)
        rgb[8:24, 8:24] = (110, 78, 56)
        mask = flood_water_mask(rgb)
        pixels = int(mask.sum())
        total = int(mask.size)
        result = {
            "possible_water_percent": round(pixels / total * 100, 2),
            "possible_water_pixels": pixels,
            "total_image_pixels": total,
            "area_estimate": None,
            "area_estimate_note": "Not calculated: verified geospatial pixel size is unavailable.",
            "method": "Multi-feature RGB, HSV and CIELAB heuristic (baseline; not trained).",
            "limitations": "Possible water only; compare pre-flood and post-flood imagery.",
            "image": png_data(rgb),
            "original_image": png_data(rgb),
            "mask_image": png_data(np.repeat((mask.astype(np.uint8) * 255)[:, :, None], 3, axis=2)),
        }
        record = SimpleNamespace(
            id="test-flood", user_id=1, kind="flood", name="PDF flood test",
            created=SimpleNamespace(isoformat=lambda: "2026-01-01T00:00:00"),
            location="", lat="", lon="", result=json.dumps(result),
        )

        class Query:
            def filter_by(self, **kwargs):
                return self

            def first(self):
                return record

        class Session:
            def query(self, model):
                self.asserted_model = model
                return Query()

        response = report("test-flood", SimpleNamespace(id=1), Session())

        async def collect_body():
            return b"".join([chunk async for chunk in response.body_iterator])

        pdf = asyncio.run(collect_body())
        self.assertTrue(pdf.startswith(b"%PDF"))
        decoded_streams = []
        for stream in re.findall(rb"stream\r?\n(.*?)~>endstream", pdf, flags=re.S):
            try:
                decoded_streams.append(zlib.decompress(base64.a85decode(stream, adobe=False)))
            except (zlib.error, ValueError):
                pass
        text = b"\n".join(decoded_streams)
        self.assertIn(b"Possible Water Percent", text)
        self.assertIn(b"Possible Water Pixels", text)
        self.assertIn(str(total).encode(), text)
        self.assertIn(b"Flood screening visual review", text)

    def test_paired_flood_pdf_contains_new_water_metrics_and_images(self):
        before = np.asarray(Image.open(ROOT / "backend" / "sample_data" / "demo_scene.png").convert("RGB"))
        after = np.asarray(Image.open(ROOT / "backend" / "sample_data" / "demo_scene_after.png").convert("RGB"))
        result = flood_comparison(before, after)
        result.update({"source": "SAMPLE DATA", "before_date": "2026-01-01", "after_date": "2026-01-02"})
        record = SimpleNamespace(
            id="paired-flood", user_id=1, kind="flood", name="Paired flood report",
            created=SimpleNamespace(isoformat=lambda: "2026-01-02T00:00:00"),
            location="", lat="", lon="", result=json.dumps(result),
        )

        class Query:
            def filter_by(self, **kwargs):
                return self

            def first(self):
                return record

        class Session:
            def query(self, model):
                return Query()

        response = report("paired-flood", SimpleNamespace(id=1), Session())

        async def collect_body():
            return b"".join([chunk async for chunk in response.body_iterator])

        pdf = asyncio.run(collect_body())
        decoded = []
        for stream in re.findall(rb"stream\r?\n(.*?)~>endstream", pdf, flags=re.S):
            try:
                decoded.append(zlib.decompress(base64.a85decode(stream, adobe=False)))
            except (ValueError, zlib.error):
                continue
        text = b"\n".join(decoded)
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertIn(b"Flood comparison visual review", text)
        self.assertIn(b"New Water Percent", text)
        self.assertIn(b"Before Flood", text)
        self.assertIn(b"After Flood", text)


if __name__ == "__main__":
    unittest.main()
