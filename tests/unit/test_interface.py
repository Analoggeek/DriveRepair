"""The interface: icons, theme, and the version reaching every artefact.

These tests are deliberately free of a display. The icon set is pure geometry plus
a painter; the geometry is checked here, the painting is checked by
``tools/preview_icons.py`` and ``tools/preview_ui.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from usbdoctor.app import icon_geometry as geometry
from usbdoctor.app.theme import DARK, LIGHT, PALETTES, palette_for, stylesheet

ROOT = Path(__file__).resolve().parents[2]


class TestIconGeometry:
    def test_every_glyph_has_strokes(self):
        for name in geometry.names():
            polylines = geometry.polylines(name, 96)
            assert polylines, f"{name} draws nothing"
            for points in polylines:
                assert len(points) >= 2, f"{name} has a degenerate stroke"

    def test_glyphs_stay_inside_their_tile(self):
        size = 64
        for name in geometry.names():
            for points in geometry.polylines(name, size):
                for x, y in points:
                    assert -0.5 <= x <= size + 0.5, f"{name} escapes horizontally at {x}"
                    assert -0.5 <= y <= size + 0.5, f"{name} escapes vertically at {y}"

    def test_the_set_covers_every_page(self):
        try:
            from usbdoctor.app.main_window import PAGES
        except ImportError as exc:  # a broken Qt install is not this test's business
            pytest.skip(f"Qt is unusable here: {exc}")

        for _key, icon_name, caption in PAGES:
            assert icon_name in geometry.names(), f"{caption} has no icon"

    def test_an_unknown_name_is_a_clear_error(self):
        with pytest.raises(KeyError) as caught:
            geometry.glyph("no-such-icon")
        assert "known:" in str(caught.value)

    def test_scaling_is_proportional(self):
        small = geometry.polylines("shield", 32)
        large = geometry.polylines("shield", 64)
        assert len(small) == len(large)
        assert large[0][0][0] == pytest.approx(small[0][0][0] * 2, abs=0.01)

    def test_stroke_width_scales_with_size(self):
        assert geometry.stroke_width(96) > geometry.stroke_width(16)
        assert geometry.stroke_width(16) >= 1.0

    @pytest.mark.parametrize("name", ["dashboard", "update", "shield", "warning"])
    def test_svg_export_is_well_formed(self, name):
        svg = geometry.to_svg(name, 18)
        assert svg.startswith("<svg") and svg.endswith("</svg>")
        assert "currentColor" in svg
        assert svg.count("<") == svg.count(">")

    def test_svg_escapes_nothing_it_should_not(self):
        # Every numeric attribute must be a number, not "inf" or "nan".
        for name in geometry.names():
            svg = geometry.to_svg(name, 24)
            assert "nan" not in svg.lower() and "inf" not in svg.lower()

    def test_the_web_navigation_uses_the_same_glyphs(self):
        html = (ROOT / "usbdoctor/frontend/web/index.html").read_text(encoding="utf-8")
        for name in ("dashboard", "scan", "repair", "settings"):
            assert geometry.to_svg(name, 18)[:60] in html, f"{name} missing from the web nav"
        # No emoji left in the navigation.
        assert not re.search(r'nav-ico">[^<]', html)


class TestTheme:
    def test_both_palettes_define_the_same_keys(self):
        assert {field for field in DARK.__dict__} == {field for field in LIGHT.__dict__}

    def test_every_colour_is_a_colour(self):
        pattern = re.compile(r"^(#[0-9a-fA-F]{6}|rgba?\([^)]+\))$")
        for palette in PALETTES.values():
            for key, value in vars(palette).items():
                if key == "name":
                    continue
                assert pattern.match(value), f"{palette.name}.{key} = {value!r}"

    def test_stylesheets_are_balanced_and_complete(self):
        for theme in ("dark", "light"):
            sheet = stylesheet(theme)
            assert sheet.count("{") == sheet.count("}")
            for selector in ("QFrame#Card", "QPushButton#Primary", "QPushButton#NavButton",
                             "QLabel#Badge--good", "QTextBrowser#ReleaseNotes",
                             "QLabel#UpdateStatus--good"):
                assert selector in sheet, f"{theme} is missing {selector}"

    def test_the_two_themes_actually_differ(self):
        assert stylesheet("dark") != stylesheet("light")

    def test_an_unknown_theme_falls_back_to_dark(self):
        assert palette_for("solarized") is DARK

    def test_the_shell_uses_object_names_the_stylesheet_defines(self):
        sheet = stylesheet("dark")
        for page in (ROOT / "usbdoctor/app/pages").glob("*.py"):
            for name in re.findall(r'setObjectName\("(\w+)"\)', page.read_text(encoding="utf-8")):
                if name.startswith("Qt"):
                    continue
                assert f"#{name}" in sheet, f"{page.name} uses #{name}, which has no rule"


class TestVersionIsSingleSourced:
    """A second hardcoded version is the one that goes stale.

    The Windows installer once declared ``1.0.0`` while the build looked for a file
    named after ``usbdoctor.version``\'s value; when those disagree the build either
    fails at the last step or ships an installer labelled with the wrong version.
    """

    DECLARATIONS = re.compile(
        r"(?:^|\s)(?:version|AppVersion|CFBundleShortVersionString|CFBundleVersion"
        r"|FileVersion|ProductVersion)\s*[:=]\s*[\"\']?\d+\.\d+\.\d+"
    )

    def _declares_a_version(self, text: str) -> list[str]:
        found = []
        for line in text.splitlines():
            if line.lstrip().startswith(("#", ";", "//")):
                continue          # a comment explains the rule, it does not break it
            if self.DECLARATIONS.search(line):
                found.append(line.strip())
        return found

    @pytest.mark.parametrize("relative", [
        "installers/windows/usbdoctor.iss",
        "installers/USBDoctorAI.spec",
        "installers/macos/USBDoctorAI.spec",
        "installers/macos/build.sh",
    ])
    def test_packaging_does_not_declare_a_version(self, relative):
        found = self._declares_a_version((ROOT / relative).read_text(encoding="utf-8"))
        assert not found, f"{relative} hardcodes a version: {found}"

    def test_the_windows_version_resource_is_generated(self):
        template = (ROOT / "installers/windows/version_info.txt").read_text(encoding="utf-8")
        spec = (ROOT / "installers/USBDoctorAI.spec").read_text(encoding="utf-8")
        assert "VERSION_FILE.write_text" in spec
        assert "__version__" in spec
        # Simulate the generation and check the result, not the template.
        version = "9.8.7"
        parts = [int(piece) for piece in (version.split(".") + ["0"] * 4)[:4]]
        generated = (template.replace("(1, 0, 0, 0)", "(" + ", ".join(map(str, parts)) + ")")
                     .replace("'1.0.0.0'", f"'{'.'.join(map(str, parts))}'"))
        assert "(9, 8, 7, 0)" in generated
        assert "'9.8.7.0'" in generated

    def test_pyproject_reads_the_version_from_the_package(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert 'dynamic = ["version"]' in text
        assert 'attr = "usbdoctor.version.__version__"' in text

    def test_the_specs_import_the_version(self):
        for spec in ("installers/USBDoctorAI.spec", "installers/macos/USBDoctorAI.spec"):
            assert "from usbdoctor.version import __version__" in (ROOT / spec).read_text(
                encoding="utf-8"), spec

    def test_the_installer_reads_the_version_from_the_build(self):
        text = (ROOT / "installers/windows/usbdoctor.iss").read_text(encoding="utf-8")
        assert "#ifndef AppVersion" in text
        build = (ROOT / "tools/build_release.py").read_text(encoding="utf-8")
        assert "/DAppVersion=" in build

    def test_the_macos_build_names_the_image_from_the_version(self):
        script = (ROOT / "installers/macos/build.sh").read_text(encoding="utf-8")
        assert "DMG=" in script and "$DMG" in script

    def test_the_interface_shows_the_running_version(self):
        assert "__version__" in (ROOT / "usbdoctor/app/pages/settings.py").read_text(encoding="utf-8")
        assert "__version__" in (ROOT / "usbdoctor/app/main_window.py").read_text(encoding="utf-8")


class TestWebUpdateEndpoint:
    """The browser interface asks the same module the dialog asks."""

    def test_the_route_exists(self):
        source = (ROOT / "usbdoctor/webapi/server.py").read_text(encoding="utf-8")
        assert '("GET", "/api/update/check"): self.get_update_check' in source

    def test_the_handler_uses_the_same_core_module(self):
        source = (ROOT / "usbdoctor/webapi/server.py").read_text(encoding="utf-8")
        assert "from usbdoctor.updates import check_for_update" in source

    def test_the_page_carries_the_controls(self):
        html = (ROOT / "usbdoctor/frontend/web/index.html").read_text(encoding="utf-8")
        assert 'id="updateCheck"' in html
        assert 'id="updateStatus"' in html

    def test_the_script_asks_the_endpoint(self):
        script = (ROOT / "usbdoctor/frontend/web/app.js").read_text(encoding="utf-8")
        assert "/api/update/check" in script
        assert "checkForUpdate" in script
