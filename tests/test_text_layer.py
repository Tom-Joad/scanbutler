from __future__ import annotations

import subprocess

import pikepdf
import pytest

from scanbutler import pdfops

from .ccitt_pdf import make_ccitt_pdf
from .conftest import make_pdf
from .test_text_source import make_text_pdf


def test_mode_choice_depends_on_existing_text():
    assert [name for name, _ in pdfops.ocr_modes(has_text=False)] == ["scan", "redo", "plain"]
    assert [name for name, _ in pdfops.ocr_modes(has_text=True)] == ["redo", "plain"]
    assert [name for name, _ in pdfops.ocr_modes(has_text=True, tagged=True)] == ["plain"]
    # --redo-ocr refuses --deskew; --clean (unpaper) is for scans only and ran
    # out of memory on a born-digital page with a high-resolution logo.
    assert "--deskew" not in pdfops.REDO_MODE[1] and "--clean" not in pdfops.REDO_MODE[1]


def test_tagged_pdf_is_kept_as_it_is(tmp_path, monkeypatch):
    src = tmp_path / "statement.pdf"
    make_text_pdf(src, ["Account statement 9/2026"])

    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        pdf.Root.MarkInfo = pikepdf.Dictionary(Marked=True)
        pdf.Root.StructTreeRoot = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.StructTreeRoot))
        pdf.save(src)
    assert pdfops.is_tagged(src)
    calls = fake_ocrmypdf(monkeypatch, [(0, "")])

    pdfops.make_searchable(src, tmp_path / "out.pdf", "deu+eng", "")

    assert len(calls) == 1 and "--skip-text" in calls[0]


def fake_ocrmypdf(monkeypatch, outcomes):
    """Replace ocrmypdf: each call pops (returncode, stderr) and records its options."""
    calls = []

    def run(cmd, capture_output, text, timeout=None):
        calls.append(cmd)
        if cmd[0] == "gs":  # downsampling: always succeeds, writes its output file
            out = next(arg.split("=", 1)[1] for arg in cmd if arg.startswith("-sOutputFile="))
            with open(out, "wb") as handle:
                handle.write(b"%PDF-1.4 downsampled")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        returncode, stderr = outcomes.pop(0)
        if returncode == "timeout":
            raise subprocess.TimeoutExpired(cmd, timeout)
        if returncode in (0, pdfops.INVALID_OUTPUT):  # 4: written, then the output check failed
            with open(cmd[-1], "wb") as handle:
                handle.write(b"%PDF-1.4 fake")
        return subprocess.CompletedProcess(cmd, returncode, "", stderr)

    monkeypatch.setattr(pdfops.subprocess, "run", run)
    return calls


def test_digital_pdf_uses_redo_ocr(tmp_path, monkeypatch):
    src = tmp_path / "statement.pdf"
    make_text_pdf(src, ["Account statement 9/2026"])
    calls = fake_ocrmypdf(monkeypatch, [(0, "")])

    pdfops.make_searchable(src, tmp_path / "out.pdf", "deu+eng", "")

    assert "--redo-ocr" in calls[0] and "--force-ocr" not in calls[0]
    assert "--quiet" not in calls[0]


def test_failed_mode_falls_back_and_reports_the_real_error(tmp_path, monkeypatch):
    src = tmp_path / "scan.pdf"
    make_pdf(src, 1)
    stderr = "Start processing\nSubprocessOutputError: unpaper: [Errno 5] Input/output error\n"
    calls = fake_ocrmypdf(monkeypatch, [(7, stderr), (0, "")])

    pdfops.make_searchable(src, tmp_path / "out.pdf", "deu+eng", "")

    assert "--force-ocr" in calls[0] and "--redo-ocr" in calls[1]
    assert (tmp_path / "out.pdf").exists()


def test_all_modes_failing_raises_with_details(tmp_path, monkeypatch):
    src = tmp_path / "scan.pdf"
    make_pdf(src, 1)
    fake_ocrmypdf(monkeypatch, [(7, "Ghostscript: [Errno 28] No space left on device"), (7, "boom"), (6, "boom")])

    with pytest.raises(RuntimeError) as failure:
        pdfops.make_searchable(src, tmp_path / "out.pdf", "deu+eng", "")

    message = str(failure.value)
    assert "scan: exit code 7" in message and "No space left on device" in message
    assert "plain: exit code 6" in message
    assert not (tmp_path / "out.pdf").exists()


def test_limits_are_passed_to_every_mode(tmp_path, monkeypatch):
    src = tmp_path / "scan.pdf"
    make_pdf(src, 1)
    calls = fake_ocrmypdf(monkeypatch, [(7, "unpaper died with SIGKILL"), (7, "x"), (0, "")])
    limits = pdfops.OcrLimits(max_ocr_mpixels=40, page_timeout=120, file_timeout_minutes=5, skip_big_mpixels=150)

    pdfops.make_searchable(src, tmp_path / "out.pdf", "deu", "", limits)

    for cmd in calls:
        assert cmd[cmd.index("--max-ocr-image-mpixels") + 1] == "40"
        assert cmd[cmd.index("--tesseract-timeout") + 1] == "120"
    # --skip-big drops OCR for a page, so it only guards the last resort.
    assert ["--skip-big" in cmd for cmd in calls] == [False, False, True]
    assert calls[2][calls[2].index("--skip-big") + 1] == "150"


def test_a_run_that_takes_too_long_falls_back(tmp_path, monkeypatch):
    src = tmp_path / "scan.pdf"
    make_pdf(src, 1)
    calls = fake_ocrmypdf(monkeypatch, [("timeout", ""), (0, "")])

    pdfops.make_searchable(src, tmp_path / "out.pdf", "deu", "", pdfops.OcrLimits(file_timeout_minutes=1))

    assert len(calls) == 2 and (tmp_path / "out.pdf").exists()


def test_limit_settings(monkeypatch):
    from scanbutler.config import Settings

    monkeypatch.setenv("MISTRAL_API_KEY", "x")
    assert Settings.from_env().ocr_limits == pdfops.OcrLimits()
    monkeypatch.setenv("OCRMYPDF_MAX_OCR_MPIXELS", "30")
    monkeypatch.setenv("OCRMYPDF_FILE_TIMEOUT_MINUTES", "45")
    limits = Settings.from_env().ocr_limits
    assert (limits.max_ocr_mpixels, limits.file_timeout_minutes) == (30, 45)


def image_pdf(path, pixels: int, dpi: int) -> None:
    from PIL import Image

    Image.new("RGB", (pixels, pixels), (255, 255, 255)).save(path, resolution=dpi)


def test_images_above_the_limit_are_downsampled_first(tmp_path, monkeypatch):
    src = tmp_path / "logo.pdf"
    image_pdf(src, 1550, 1550)  # one square inch at 1550 dpi
    assert round(pdfops.max_image_dpi(src)) == 1550
    calls = fake_ocrmypdf(monkeypatch, [(0, "")])

    pdfops.make_searchable(src, tmp_path / "out.pdf", "deu", "")

    gs, ocr = calls
    assert gs[0] == "gs" and "-dColorImageResolution=600" in gs
    assert ocr[-2].endswith(".prepared.pdf")  # ocrmypdf reads the downsampled copy
    assert not list(tmp_path.glob("*.prepared.pdf"))  # and it is cleaned up


def test_normal_scans_are_not_rewritten(tmp_path, monkeypatch):
    src = tmp_path / "scan.pdf"
    image_pdf(src, 600, 300)
    calls = fake_ocrmypdf(monkeypatch, [(0, "")])

    pdfops.make_searchable(src, tmp_path / "out.pdf", "deu", "")

    assert len(calls) == 1 and calls[0][0] == "ocrmypdf" and calls[0][-2] == str(src)


def test_downsampling_can_be_switched_off(tmp_path, monkeypatch):
    src = tmp_path / "logo.pdf"
    image_pdf(src, 1550, 1550)
    calls = fake_ocrmypdf(monkeypatch, [(0, "")])

    pdfops.make_searchable(src, tmp_path / "out.pdf", "deu", "", pdfops.OcrLimits(max_image_dpi=0))

    assert [cmd[0] for cmd in calls] == ["ocrmypdf"]


def test_downsampling_has_a_time_limit(tmp_path, monkeypatch):
    def hang(cmd, capture_output, text, timeout=None):
        assert timeout == 60
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(pdfops.subprocess, "run", hang)
    with pytest.raises(RuntimeError, match="did not finish"):
        pdfops.downsample_images(tmp_path / "in.pdf", tmp_path / "out.pdf", 600, timeout=60)


UNDECODABLE = (
    "Postprocessing...\n"
    "ERROR: stream 12 0 R (/CCITTFaxDecode) could not be decoded: /CCITTFaxDecode without /DecodeParms\n"
    "Output file: The generated PDF is INVALID\n"
)


def test_output_with_the_inputs_own_undecodable_images_is_kept(tmp_path, monkeypatch, caplog):
    src = tmp_path / "fax.pdf"
    make_ccitt_pdf(src)
    calls = fake_ocrmypdf(monkeypatch, [(pdfops.INVALID_OUTPUT, UNDECODABLE)])

    with caplog.at_level("WARNING"):
        pdfops.make_searchable(src, tmp_path / "out.pdf", "deu+eng", "")

    assert len(calls) == 1 and (tmp_path / "out.pdf").exists()
    done = next(r for r in caplog.records if r.message == "text layer done")
    assert done.levelname == "WARNING"
    assert done.input_streams_unreadable == ["/CCITTFaxDecode without /DecodeParms"]


def test_undecodable_output_from_a_clean_input_still_fails(tmp_path, monkeypatch):
    src = tmp_path / "scan.pdf"
    make_pdf(src, 1)
    calls = fake_ocrmypdf(monkeypatch, [(pdfops.INVALID_OUTPUT, UNDECODABLE)] * 3)

    with pytest.raises(RuntimeError, match="exit code 4"):
        pdfops.make_searchable(src, tmp_path / "out.pdf", "deu+eng", "")

    assert len(calls) == 3 and not (tmp_path / "out.pdf").exists()


def test_other_errors_next_to_inherited_ones_are_not_accepted(tmp_path):
    src = tmp_path / "fax.pdf"
    make_ccitt_pdf(src)
    assert pdfops.inherited_problems(UNDECODABLE, src) == ["/CCITTFaxDecode without /DecodeParms"]
    assert pdfops.inherited_problems(UNDECODABLE + "ERROR: page 1: content stream is broken\n", src) is None
    other = UNDECODABLE.replace("without /DecodeParms", "with unsupported /K")
    assert pdfops.inherited_problems(other, src) is None  # a problem the input doesn't have
    assert pdfops.inherited_problems("Output file: The generated PDF is INVALID\n", src) is None
