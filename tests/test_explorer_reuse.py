import unittest
from pathlib import Path
from unittest import mock

import explorer_reuse
from explorer_reuse import ExplorerWindow


class ExplorerReuseTests(unittest.TestCase):
    def test_exact_match_is_case_insensitive_and_normalized(self):
        windows = [
            ExplorerWindow(41, Path(r"C:\Prints\FuzzyPaint Output")),
        ]
        match = explorer_reuse.find_exact_folder_window(
            Path(r"c:\prints\FUZZYPAINT OUTPUT\."), windows
        )
        self.assertEqual(match, windows[0])

    def test_parent_or_child_folder_is_not_an_exact_match(self):
        windows = [ExplorerWindow(41, Path(r"C:\Prints"))]
        match = explorer_reuse.find_exact_folder_window(
            Path(r"C:\Prints\FuzzyPaint Output"), windows
        )
        self.assertIsNone(match)

    @mock.patch("explorer_reuse.subprocess.Popen")
    @mock.patch("explorer_reuse.focus_explorer_window")
    @mock.patch("explorer_reuse.enumerate_explorer_windows")
    def test_reveal_focuses_existing_exact_folder_without_opening_another(
        self, enumerate_windows, focus_window, popen
    ):
        enumerate_windows.return_value = [
            ExplorerWindow(73, Path(r"C:\Prints\FuzzyPaint Output"))
        ]

        explorer_reuse.reveal_in_explorer(
            Path(r"c:\prints\fuzzypaint output\result.3mf")
        )

        focus_window.assert_called_once_with(73)
        popen.assert_not_called()

    @mock.patch("explorer_reuse.subprocess.Popen")
    @mock.patch("explorer_reuse.focus_explorer_window")
    @mock.patch("explorer_reuse.enumerate_explorer_windows")
    def test_reveal_opens_folder_when_no_exact_window_exists(
        self, enumerate_windows, focus_window, popen
    ):
        enumerate_windows.return_value = [ExplorerWindow(73, Path(r"C:\Prints"))]

        explorer_reuse.reveal_in_explorer(
            Path(r"C:\Prints\FuzzyPaint Output\result.3mf")
        )

        focus_window.assert_not_called()
        popen.assert_called_once_with(
            ["explorer.exe", r"C:\Prints\FuzzyPaint Output"]
        )

    @mock.patch("explorer_reuse.subprocess.run")
    def test_enumeration_parses_valid_windows_and_ignores_bad_entries(self, run):
        run.return_value = mock.Mock(
            stdout='[{"hwnd":12,"path":"C:\\\\Prints"},{"hwnd":"bad"}]'
        )
        self.assertEqual(
            explorer_reuse.enumerate_explorer_windows(),
            [ExplorerWindow(12, Path(r"C:\Prints"))],
        )


if __name__ == "__main__":
    unittest.main()
