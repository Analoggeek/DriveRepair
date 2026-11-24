"""The update dialog, and the thread that keeps the window responsive.

Two rules shape this file. Nothing here blocks the GUI thread — a check has an
eight-second timeout and a download can take a minute, and either one freezing the
window would look exactly like the crash it is not. And nothing here installs
anything: the dialog finds the release, verifies the file, and then opens it with
the operating system, so Windows shows its own installer and macOS its own disk
image. The application never replaces its own binary behind the user's back.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QSize, QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from usbdoctor.app import icons
from usbdoctor.app.theme import stylesheet
from usbdoctor.service.engine import DoctorService
from usbdoctor.updates import (
    UpdateCheck,
    format_notes_html,
    UpdateError,
    check_for_update,
    default_download_dir,
    download_update,
    open_downloaded,
)
from usbdoctor.version import APP_NAME, __version__


class UpdateWorker(QThread):
    """Runs one callable off the GUI thread and reports back with signals."""

    finished = Signal(object)   # noqa: A003 - Qt's name
    failed = Signal(str)

    def __init__(self, work: Callable[[], object], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._work = work

    def run(self) -> None:  # noqa: D102 - Qt entry point
        try:
            self.finished.emit(self._work())
        except UpdateError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - a thread must never die silently
            self.failed.emit(f"The update check failed unexpectedly: {exc}")


class DownloadWorker(QThread):
    """Streams one asset, with progress, off the GUI thread."""

    progressed = Signal(int, int)
    finished = Signal(object)   # noqa: A003 - Qt's name
    failed = Signal(str)

    def __init__(self, check: UpdateCheck, target: Path,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._check = check
        self._target = target

    def run(self) -> None:  # noqa: D102 - Qt entry point
        try:
            path = download_update(self._check, self._target,
                                   progress=lambda done, total: self.progressed.emit(done, total))
            self.finished.emit(path)
        except UpdateError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"The download failed: {exc}")


class UpdateDialog(QDialog):
    """What is new, how big it is, and one button that fetches it."""

    def __init__(self, service: DoctorService, *, parent: Optional[QWidget] = None,
                 theme: str = "dark") -> None:
        super().__init__(parent)
        self.service = service
        self.setWindowTitle(f"{APP_NAME} — Updates")
        self.setStyleSheet(stylesheet(theme))
        self.setMinimumWidth(620)
        self.setModal(True)
        self.result_check: Optional[UpdateCheck] = None
        self.downloaded: Optional[Path] = None
        self._worker: Optional[QThread] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(13)

        header = QHBoxLayout()
        header.setSpacing(13)
        tile = QLabel()
        pixmap = icons.app_icon().pixmap(QSize(46, 46))
        if not pixmap.isNull():
            tile.setPixmap(pixmap)
        header.addWidget(tile)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        title = QLabel("Updates")
        title.setObjectName("PageTitle")
        subtitle = QLabel(f"Installed version {__version__}  ·  {APP_NAME}")
        subtitle.setObjectName("PageSub")
        titles.addWidget(title)
        titles.addWidget(subtitle)
        header.addLayout(titles)
        header.addStretch(1)
        layout.addLayout(header)

        self.status = QLabel("Checking for the latest release…")
        self.status.setObjectName("UpdateStatus")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.notes = QTextBrowser()
        self.notes.setObjectName("ReleaseNotes")
        self.notes.setOpenExternalLinks(True)
        self.notes.setMinimumHeight(220)
        self.notes.setHtml(
            "<p style='color:#8b98ab'>Release notes will appear here once the check "
            "finishes.</p>"
        )
        layout.addWidget(self.notes, 1)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        buttons = QHBoxLayout()
        buttons.setSpacing(9)
        self.release_button = QPushButton("Open release page")
        self.release_button.setObjectName("Ghost")
        self.release_button.clicked.connect(self.open_release_page)
        self.release_button.setEnabled(False)
        buttons.addWidget(self.release_button)
        buttons.addStretch(1)
        self.download_button = QPushButton("Download update")
        self.download_button.setObjectName("Primary")
        self.download_button.setVisible(False)
        self.download_button.clicked.connect(self.start_download)
        buttons.addWidget(self.download_button)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.accept)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

        self.check_now()

    # ------------------------------------------------------------------ state
    def show_result(self, check: UpdateCheck) -> None:
        """Render a check that somebody else already made (the startup check)."""
        if self._worker is not None:
            return  # a live check will report in a moment; do not fight it
        self._render(check)

    def check_now(self) -> None:
        self._set_status("Checking for the latest release…", "busy")
        self.download_button.setVisible(False)
        self._worker = UpdateWorker(check_for_update, self)
        self._worker.finished.connect(self._on_checked)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_checked(self, result: object) -> None:
        self._worker = None
        self._render(result)  # type: ignore[arg-type]

    def _on_failed(self, message: str) -> None:
        self._worker = None
        self._set_status(message, "bad")
        self.notes.setHtml(
            "<p>Updates need a network connection; everything else in this "
            "application works without one.</p>"
        )

    def _set_status(self, text: str, tone: str = "") -> None:
        self.status.setObjectName("UpdateStatus" + (f"--{tone}" if tone else ""))
        self.status.setText(text)
        # Object names are what the stylesheet keys on; a changed name needs a
        # repolish or Qt keeps painting the old colours.
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)

    def _render(self, check: UpdateCheck) -> None:
        self.result_check = check
        # Reset first: a check that fails after a successful one used to leave
        # "Download 67.7 MB" sitting there pointing at nothing.
        self.download_button.setVisible(False)
        self.download_button.setEnabled(False)
        self.progress.setVisible(False)
        self.release_button.setEnabled(bool(check.release and check.release.url))
        if check.status == "unavailable":
            self._set_status(check.error or "The update check could not complete.", "bad")
            self.notes.setHtml(self._html_offline(check))
            return
        if not check.update_available:
            self._set_status(check.message or f"You are on the newest version ({__version__}).",
                             "good")
            self.notes.setHtml(self._html_up_to_date(check))
            return
        self._set_status(check.message, "good")
        self.notes.setHtml(self._html_release(check))
        if check.asset is not None:
            self.download_button.setText(f"Download {check.asset.human_size}")
            self.download_button.setVisible(True)
            self.download_button.setEnabled(True)
        else:
            self.download_button.setVisible(False)

    # ------------------------------------------------------------------- html
    def _html_offline(self, check: UpdateCheck) -> str:
        hint = f"<p style='color:#8b98ab'>{_escape(check.hint)}</p>" if check.hint else ""
        return (f"<h3>Could not check for updates</h3>"
                f"<p>{_escape(check.error or '')}</p>{hint}"
                "<p style='color:#8b98ab'>You can still download the newest build by hand "
                "from the project's releases page.</p>")

    def _html_up_to_date(self, check: UpdateCheck) -> str:
        return (f"<h3>You are up to date</h3>"
                f"<p>Installed: <b>{_escape(check.current)}</b><br>"
                f"Published: <b>{_escape(check.latest or check.current)}</b></p>"
                "<p style='color:#8b98ab'>Nothing needs downloading.</p>")

    def _html_release(self, check: UpdateCheck) -> str:
        release = check.release
        if release is None:  # pragma: no cover - guarded by callers
            return ""
        published = release.published_at.replace("T", " ").replace("Z", "")[:16]
        asset = check.asset
        asset_line = (f"<p>Your download: <b>{_escape(asset.name)}</b> "
                      f"({asset.human_size}) for {_escape(check.platform_tag)}</p>"
                      if asset else "")
        paragraphs = (format_notes_html(release.notes) if release.notes.strip()
                      else "<p><em>No release notes were attached.</em></p>")
        return (f"<h3>{_escape(release.name or release.tag)}</h3>"
                f"<p style='color:#8b98ab'>Published {_escape(published)}  ·  "
                f"you have {_escape(check.current)}</p>"
                f"{asset_line}{paragraphs}")

    # --------------------------------------------------------------- download
    def start_download(self) -> None:
        if self.result_check is None or self.result_check.asset is None:
            return
        self.download_button.setEnabled(False)
        self.download_button.setText("Downloading…")
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)  # busy until the first size hint arrives
        self._set_status("Downloading…", "busy")
        target = default_download_dir() / "USBDoctorAI-updates"
        self._worker = DownloadWorker(self.result_check, target, self)
        self._worker.progressed.connect(self._on_progress)  # type: ignore[attr-defined]
        self._worker.finished.connect(self._on_downloaded)
        self._worker.failed.connect(self._on_download_failed)
        self._worker.start()

    def _on_progress(self, done: int, total: int) -> None:
        if total <= 0:
            return
        self.progress.setRange(0, 100)
        self.progress.setValue(int(done * 100 / total))

    def _on_downloaded(self, path: object) -> None:
        self._worker = None
        self.downloaded = Path(str(path))
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        verified = " (checksum verified)" if self.result_check and self._has_checksums() else ""
        self._set_status(f"Saved to {self.downloaded}{verified}.", "good")
        self.download_button.setText("Open the installer")
        self.download_button.setEnabled(True)
        try:
            self.download_button.clicked.disconnect()
        except (RuntimeError, TypeError):
            pass
        self.download_button.clicked.connect(self.open_downloaded_file)

    def _has_checksums(self) -> bool:
        check = self.result_check
        if check is None or check.release is None:
            return False
        from usbdoctor.updates import checksum_name

        return check.release.asset(checksum_name(check.platform_tag)) is not None

    def _on_download_failed(self, message: str) -> None:
        self._worker = None
        self.progress.setVisible(False)
        self.download_button.setEnabled(True)
        self.download_button.setText("Try the download again")
        self._set_status(message, "bad")

    def open_downloaded_file(self) -> None:
        if self.downloaded is None:
            return
        if not open_downloaded(self.downloaded):
            self._set_status(f"Saved to {self.downloaded} — open it from your file manager.",
                             "good")
            return
        self._set_status(f"Opened {self.downloaded.name}. Your operating system will "
                         "ask before anything is installed.", "good")

    def open_release_page(self) -> None:
        if self.result_check and self.result_check.release and self.result_check.release.url:
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices

            QDesktopServices.openUrl(QUrl(self.result_check.release.url))

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._worker is not None and self._worker.isRunning():
            self._worker.wait(3000)
        super().closeEvent(event)


def _escape(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
