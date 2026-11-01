"""Filename cleaning / branding tests (brief section 5 and section 18)."""

from django.test import SimpleTestCase

from uploader.services.naming import (
    FilenameError,
    clean_filename,
    preview_variants,
    split_extension,
)


class SplitExtensionTests(SimpleTestCase):
    def test_normal(self):
        self.assertEqual(split_extension("Movie.Name.mkv"), ("Movie.Name", "mkv"))

    def test_uppercase_extension_is_lowered(self):
        self.assertEqual(split_extension("Movie.MP4"), ("Movie", "mp4"))

    def test_no_extension(self):
        self.assertEqual(split_extension("Movie"), ("Movie", ""))

    def test_trailing_dot_is_not_an_extension(self):
        self.assertEqual(split_extension("Movie."), ("Movie", ""))


class AutoModeTests(SimpleTestCase):
    def test_documented_example(self):
        """The exact example from the brief must be reproduced byte for byte."""
        result = clean_filename(
            "Extraction-3-2026-nkiri.com.mkv",
            title="Extraction 3",
            mode="auto",
            brand_rules=[("token", "nkiri")],
        )
        self.assertEqual(result.filename, "extraction-3-2026 | NZDWorld.com.mkv")
        self.assertEqual(result.extension, "mkv")
        self.assertIn("2026", result.preserved)
        self.assertIn("nkiri.com", result.removed)

    def test_extension_is_preserved_per_container(self):
        for ext in ("mp4", "mkv", "webm"):
            with self.subTest(ext=ext):
                result = clean_filename(
                    f"Some_Movie_2024_site.com.{ext}", title="Some Movie", mode="auto"
                )
                self.assertTrue(result.filename.endswith(f".{ext}"))

    def test_year_comes_from_source_when_title_lacks_it(self):
        result = clean_filename(
            "Dune.Part.Two.2024.1080p.WEB-DL.x264-GROUP.mkv",
            title="Dune Part Two",
            mode="auto",
        )
        self.assertTrue(result.filename.startswith("dune-part-two-2024-1080p"))
        self.assertTrue(result.filename.endswith(" | NZDWorld.com.mkv"))

    def test_episode_marker_is_preserved(self):
        result = clean_filename(
            "Tulsa.King.S03E01.720p.HDTV.x264.mkv",
            title="Tulsa King",
            mode="auto",
        )
        self.assertIn("s03e01", result.filename)
        self.assertIn("720p", result.filename)

    def test_edition_token_is_preserved(self):
        result = clean_filename(
            "Blade.Runner.2049.2017.Extended.1080p.mkv",
            title="Blade Runner 2049",
            mode="auto",
        )
        self.assertIn("extended", result.filename)
        # The year token in the title must not be duplicated.
        self.assertEqual(result.filename.count("2049"), 1)

    def test_missing_title_is_an_error(self):
        with self.assertRaises(FilenameError):
            clean_filename("Movie.mkv", title="", mode="auto")

    def test_disallowed_extension_is_an_error(self):
        with self.assertRaises(FilenameError):
            clean_filename(
                "movie.exe", title="Movie", mode="auto", allowed_extensions=["mp4", "mkv"]
            )

    def test_accented_title_is_folded_to_ascii(self):
        result = clean_filename("Amelie.2001.mkv", title="Amélie", mode="auto")
        self.assertEqual(result.filename, "amelie-2001 | NZDWorld.com.mkv")


class ManualModeTests(SimpleTestCase):
    def test_manual_cleaning_matches_auto_branding(self):
        result = clean_filename(
            "Extraction 3 2026 nkiri.com.mkv",
            mode="manual",
            brand_rules=[("token", "nkiri")],
        )
        self.assertEqual(result.filename, "extraction-3-2026 | NZDWorld.com.mkv")

    def test_existing_suffix_is_not_duplicated(self):
        for name in (
            "extraction-3-2026 | NZDWorld.com.mkv",
            "extraction-3-2026-NZDWorld.com.mkv",
            "extraction 3 2026 nzdworld.com.mkv",
        ):
            with self.subTest(name=name):
                result = clean_filename(name, mode="manual")
                self.assertEqual(result.filename.count("NZDWorld.com"), 1)
                self.assertEqual(result.filename, "extraction-3-2026 | NZDWorld.com.mkv")

    def test_manual_text_extension_is_replaced_by_real_one(self):
        """The admin typing '.mp4' must not change the actual container."""
        result = clean_filename("My Movie.mp4", mode="manual")
        self.assertTrue(result.filename.endswith(".mp4"))

    def test_path_traversal_is_neutralised(self):
        result = clean_filename("../../etc/passwd.mkv", mode="manual")
        self.assertNotIn("/", result.filename)
        self.assertNotIn("..", result.filename)
        self.assertTrue(result.filename.startswith("etc-passwd"))

    def test_control_characters_are_stripped(self):
        result = clean_filename("Mov\x00ie\x1f 2024.mkv", mode="manual")
        self.assertNotIn("\x00", result.filename)
        self.assertNotIn("\x1f", result.filename)

    def test_empty_after_cleaning_is_an_error(self):
        with self.assertRaises(FilenameError):
            clean_filename("NZDWorld.com.mkv", mode="manual")


class BrandRuleSafetyTests(SimpleTestCase):
    def test_token_rule_does_not_damage_similar_words(self):
        """A rule for 'nkiri' must not eat 'Nkirika' inside a real title."""
        result = clean_filename(
            "Nkirika.The.Movie.2024.mkv",
            title="Nkirika The Movie",
            mode="auto",
            brand_rules=[("token", "nkiri")],
        )
        self.assertIn("nkirika", result.filename)

    def test_regex_rule_is_applied(self):
        result = clean_filename(
            "Movie.2024.[www.example.com].mkv",
            title="Movie",
            mode="auto",
            brand_rules=[("regex", r"^www$|^example$")],
        )
        self.assertNotIn("example", result.filename)

    def test_inactive_rule_objects_are_ignored(self):
        class Rule:
            kind = "token"
            pattern = "keepme"
            is_active = False

        result = clean_filename(
            "Keepme Movie 2024.mkv", title="Keepme Movie", mode="auto", brand_rules=[Rule()]
        )
        self.assertIn("keepme", result.filename)

    def test_custom_suffix(self):
        result = clean_filename("Movie 2024.mkv", title="Movie", mode="auto", brand_suffix="Example.org")
        self.assertEqual(result.filename, "movie-2024 | Example.org.mkv")


class LengthAndPreviewTests(SimpleTestCase):
    def test_stem_is_truncated_on_a_word_boundary(self):
        long_title = " ".join(["word"] * 60)
        result = clean_filename("x.2024.mkv", title=long_title, mode="auto")
        stem = result.filename.rsplit(" | ", 1)[0]
        self.assertLessEqual(len(stem), 140)
        self.assertFalse(stem.endswith("-"))

    def test_preview_returns_both_variants(self):
        out = preview_variants(
            "Extraction-3-2026-nkiri.com.mkv",
            "Extraction 3",
            manual_name="Extraction 3 2026.mkv",
            brand_rules=[("token", "nkiri")],
        )
        self.assertEqual(out["auto"], "extraction-3-2026 | NZDWorld.com.mkv")
        self.assertEqual(out["manual"], "extraction-3-2026 | NZDWorld.com.mkv")
        self.assertEqual(out["errors"], "")

    def test_preview_reports_errors_without_raising(self):
        out = preview_variants("noextension", "Title")
        self.assertIn("auto", out["errors"])
        self.assertEqual(out["auto"], "")
