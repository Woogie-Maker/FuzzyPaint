import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from bambu_cli import BambuSlicingError, archive_has_gcode, build_slice_command, slice_project


class BambuCliTests(unittest.TestCase):
    def test_archive_has_gcode_distinguishes_project_from_slice(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project.3mf"
            sliced = Path(directory) / "sliced.3mf"
            with zipfile.ZipFile(project, "w") as archive:
                archive.writestr("3D/3dmodel.model", "model")
            with zipfile.ZipFile(sliced, "w") as archive:
                archive.writestr("Metadata/plate_1.gcode", "G1 X0 Y0")
            self.assertFalse(archive_has_gcode(project))
            self.assertTrue(archive_has_gcode(sliced))

    def test_build_slice_command_uses_minimal_documented_form(self):
        command = build_slice_command(Path("bambu-studio.exe"), Path("input.3mf"), Path("output.3mf"))
        self.assertEqual(
            command,
            [
                "bambu-studio.exe", "--slice", "0", "--debug", "2",
                "--export-3mf", "output.3mf", "input.3mf",
            ],
        )

    def test_slice_project_rejects_success_without_gcode_output(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.3mf"
            destination = Path(directory) / "output.3mf"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("3D/3dmodel.model", "model")
            with mock.patch("bambu_cli.subprocess.run") as run:
                run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
                with self.assertRaises(BambuSlicingError):
                    slice_project(source, destination, executable=Path("bambu-studio.exe"))


if __name__ == "__main__":
    unittest.main()
