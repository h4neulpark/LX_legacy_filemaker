import tempfile
import unittest
from pathlib import Path

import app


class LegalDongToolTests(unittest.TestCase):
    def test_load_legal_dongs_filters_existing_rows(self) -> None:
        text = (
            "법정동코드\t법정동명\t폐지여부\n"
            "1111010100\t서울특별시 종로구 청운동\t존재\n"
            "1111010200\t서울특별시 종로구 신교동\t폐지\n"
            "1111010300\t서울특별시 종로구 궁정동\t\n"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "codes.txt"
            path.write_text(text, encoding="utf-8")
            dongs = app.load_legal_dongs(path)

        self.assertEqual(len(dongs), 1)
        self.assertEqual(dongs[0].code, "1111010100")

    def test_search_handles_partial_and_spaced_query(self) -> None:
        dongs = [
            app.LegalDong("1168010100", "서울특별시 강남구 역삼동", "서울특별시강남구역삼동", ("서울특별시", "강남구", "역삼동")),
            app.LegalDong("1111010100", "서울특별시 종로구 청운동", "서울특별시종로구청운동", ("서울특별시", "종로구", "청운동")),
        ]

        result = app.search_legal_dongs("강남 역삼 123-4", dongs)

        self.assertEqual(result[0].code, "1168010100")

    def test_parse_lot_number_regular_without_sub(self) -> None:
        lot = app.parse_lot_number("서울 강남구 역삼동 123")

        self.assertIsNotNone(lot)
        self.assertFalse(lot.is_mountain)
        self.assertEqual(lot.main, 123)
        self.assertEqual(lot.sub, 0)
        self.assertEqual(app.make_pnu("1168010100", lot), "1168010100101230000")

    def test_parse_lot_number_regular_with_sub(self) -> None:
        lot = app.parse_lot_number("서울 강남구 역삼동 123-4")

        self.assertIsNotNone(lot)
        self.assertEqual(lot.main, 123)
        self.assertEqual(lot.sub, 4)
        self.assertEqual(app.make_pnu("1168010100", lot), "1168010100101230004")

    def test_parse_lot_number_mountain(self) -> None:
        lot = app.parse_lot_number("경기 성남시 분당구 정자동 산12-3")

        self.assertIsNotNone(lot)
        self.assertTrue(lot.is_mountain)
        self.assertEqual(app.make_pnu("4113510300", lot), "4113510300200120003")

    def test_parse_lot_number_missing(self) -> None:
        self.assertIsNone(app.parse_lot_number("서울 강남구 역삼동"))

    def test_build_destination_path_sanitizes_and_plans_split_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            folder = Path(tmpdir)
            source = folder / "source.pdf"
            source.write_text("x", encoding="utf-8")
            existing = folder / "2026_실태조사서_1168010100101230004_서울특별시_강남구_역삼동_123-4.pdf"
            existing.write_text("old", encoding="utf-8")

            plan = app.build_destination_plan(
                source,
                folder,
                "2026",
                "실태조사서",
                "1168010100101230004",
                "서울특별시 강남구 역삼동 123-4",
            )
            destination = app.build_destination_path(
                source,
                folder,
                "2026",
                "실태조사서",
                "1168010100101230004",
                "서울특별시 강남구 역삼동 123-4",
            )

        self.assertEqual(destination.name, "2026_실태조사서_1168010100101230004_서울특별시_강남구_역삼동_123-4__2.pdf")
        self.assertEqual(plan.destination.name, "2026_실태조사서_1168010100101230004_서울특별시_강남구_역삼동_123-4__2.pdf")
        self.assertEqual(
            [(old.name, new.name) for old, new in plan.renames],
            [
                (
                    "2026_실태조사서_1168010100101230004_서울특별시_강남구_역삼동_123-4.pdf",
                    "2026_실태조사서_1168010100101230004_서울특별시_강남구_역삼동_123-4__1.pdf",
                )
            ],
        )

    def test_copy_with_destination_plan_renames_existing_base_and_copies_to_two(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output = root / "out"
            output.mkdir()
            source = root / "source.pdf"
            source.write_text("new", encoding="utf-8")
            existing = output / "2026_실태조사서_1168010100101230004_서울특별시_강남구_역삼동_123-4.pdf"
            existing.write_text("old", encoding="utf-8")

            plan = app.build_destination_plan(
                source,
                output,
                "2026",
                "실태조사서",
                "1168010100101230004",
                "서울특별시 강남구 역삼동 123-4",
            )
            app.copy_with_destination_plan(source, plan)

            first = output / "2026_실태조사서_1168010100101230004_서울특별시_강남구_역삼동_123-4__1.pdf"
            second = output / "2026_실태조사서_1168010100101230004_서울특별시_강남구_역삼동_123-4__2.pdf"
            self.assertFalse(existing.exists())
            self.assertEqual(first.read_text(encoding="utf-8"), "old")
            self.assertEqual(second.read_text(encoding="utf-8"), "new")

    def test_destination_plan_uses_next_number_when_base_was_already_split(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            folder = Path(tmpdir)
            source = folder / "source.pdf"
            source.write_text("x", encoding="utf-8")
            stem = "2026_실태조사서_1168010100101230004_서울특별시_강남구_역삼동_123-4"
            (folder / f"{stem}__1.pdf").write_text("old", encoding="utf-8")
            (folder / f"{stem}__2.pdf").write_text("new", encoding="utf-8")

            destination = app.build_destination_path(
                source,
                folder,
                "2026",
                "실태조사서",
                "1168010100101230004",
                "서울특별시 강남구 역삼동 123-4",
            )

        self.assertEqual(destination.name, "2026_실태조사서_1168010100101230004_서울특별시_강남구_역삼동_123-4__3.pdf")

    def test_list_pdf_files_only_direct_pdfs_case_insensitive(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            folder = Path(tmpdir)
            (folder / "b.PDF").write_text("x", encoding="utf-8")
            (folder / "a.pdf").write_text("x", encoding="utf-8")
            (folder / "note.txt").write_text("x", encoding="utf-8")
            sub = folder / "sub"
            sub.mkdir()
            (sub / "nested.pdf").write_text("x", encoding="utf-8")

            result = app.list_pdf_files(folder)

        self.assertEqual([path.name for path in result], ["a.pdf", "b.PDF"])

    def test_source_pdf_counts_returns_total_processed_and_remaining(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            folder = Path(tmpdir)
            done = folder / "done.pdf"
            pending = folder / "pending.pdf"
            done.write_text("x", encoding="utf-8")
            pending.write_text("x", encoding="utf-8")

            counts = app.source_pdf_counts([done, pending], {done.resolve()})

        self.assertEqual(counts, (2, 1, 1))
    def test_list_output_files_sorts_direct_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            folder = Path(tmpdir)
            (folder / "z.pdf").write_text("x", encoding="utf-8")
            (folder / "A.pdf").write_text("x", encoding="utf-8")
            (folder / "sub").mkdir()

            result = app.list_output_files(folder)

        self.assertEqual([path.name for path in result], ["A.pdf", "z.pdf"])

    def test_mark_processed_name_uses_resolved_source_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "source.pdf"
            source.write_text("x", encoding="utf-8")
            processed = {source.resolve()}

            self.assertEqual(app.mark_processed_name(source, processed), "[완료] source.pdf")
            self.assertEqual(app.mark_processed_name(source, set()), "[대기] source.pdf")


    def test_work_state_save_and_load_round_trips_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            folder = Path(tmpdir)
            pdf = folder / "source.pdf"
            pdf.write_text("x", encoding="utf-8")
            output = folder / "out"
            output.mkdir()
            state = app.build_work_state(folder, output, {pdf.resolve()}, pdf, "2026", "실태조사서")

            app.save_work_state(folder, state)
            loaded = app.load_work_state(folder)

        self.assertEqual(loaded["processed_files"], ["source.pdf"])
        self.assertEqual(loaded["last_selected_pdf"], "source.pdf")
        self.assertEqual(loaded["year"], "2026")
        self.assertEqual(loaded["doc_type"], "실태조사서")

    def test_processed_sources_from_state_restores_existing_direct_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            folder = Path(tmpdir)
            done = folder / "done.pdf"
            pending = folder / "pending.pdf"
            done.write_text("x", encoding="utf-8")
            pending.write_text("x", encoding="utf-8")
            state = {"processed_files": ["done.pdf", "missing.pdf"]}

            restored = app.processed_sources_from_state(folder, [done, pending], state)

        self.assertEqual(restored, {done.resolve()})

    def test_load_work_state_returns_empty_for_broken_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            folder = Path(tmpdir)
            app.work_state_path(folder).write_text("{broken", encoding="utf-8")

            loaded = app.load_work_state(folder)

        self.assertEqual(loaded, {})

if __name__ == "__main__":
    unittest.main()



