#!/bin/env python3
from __future__ import annotations

import signal
import atexit
import sys
import argparse
import multiprocessing
import subprocess
import shutil
import os.path
import time
import datetime
import re

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TypeAlias


Argparse: TypeAlias = argparse.Namespace
CompletedProcess: TypeAlias = subprocess.CompletedProcess


class File:
    """Transforms a Path to File object with additional attributes."""

    def __init__(
        self,
        input_path: Path,
        dir_path: Path | None = None,
        temp_path: Path | None = None,
    ):
        """Constructs File attributes."""

        self.input: Path = input_path
        if dir_path:
            output = dir_path.joinpath(input_path.name)
        else:
            output = input_path
        self.output: Path = output.with_suffix(".chd")
        self.type: str | None = App.match_type(self.input)
        self.tempdir: TemporaryDirectory | None = None
        if self.type == "archive":
            # TODO: add option to use default temp path (see description of dir for how a default directory is chosen:
            #  https://docs.python.org/3/library/tempfile.html#tempfile.mkstemp)
            if temp_path:
                self.tempdir = TemporaryDirectory(dir=temp_path)
            else:
                self.tempdir = TemporaryDirectory(dir=self.output.parent)

    def get_size(self, unit: str = "B"):
        exponents_map = {"B": 0, "KB": 1, "MB": 2, "GB": 3}
        if unit not in exponents_map:
            raise ValueError(f"Unit must be one of: {list(exponents_map.keys())}")

        file_size = self.input.stat().st_size
        if unit == "bytes":
            return file_size

        size = file_size / 1024 ** exponents_map[unit]
        return round(size, 3)


def filter_other_in_gdi_dirs(list_of_files: list[File]):
    """Remove all other files from list within same dir as .gdi."""

    gdi_dirs = [f.input.parent for f in list_of_files if f.input.suffix == ".gdi"]
    if gdi_dirs:
        filtered_list: list[File] = []
        for file in list_of_files:
            if file.input.parent not in gdi_dirs or file.input.suffix == ".gdi":
                # BUG: If the .gdi file contains lines that are not actual
                # existing files, then this can lead to removing of all
                # other files from queue and stop processing.
                filtered_list.append(file)
        return filtered_list
    else:
        return list_of_files


def filter_images_in_sheet_dirs(list_of_files: list[File]):
    """Remove all image files from list within same dir as sheets."""

    sheet_dirs = [f.input.parent for f in list_of_files if f.type == "sheet"]
    if sheet_dirs:
        filtered_list: list[File] = []
        for file in list_of_files:
            if file.input.parent in sheet_dirs and file.input.suffix == ".iso":
                continue
            else:
                filtered_list.append(file)
        return filtered_list
    else:
        return list_of_files


class App:
    """Contains all settings and meta information for the application."""

    name: str = "tochd"
    version: str = "0.12"
    types: dict[str, tuple[str, ...]] = {
        "sheet": (
            "gdi",
            "cue",
        ),
        "image": ("iso",),
        "archive": (
            "7z",
            "zip",
            "gz",
            "gzip",
            "bz2",
            "bzip2",
            "rar",
            "tar",
        ),
    }
    exclude_hidden = True
    home_as_posix: str = Path("~").expanduser().as_posix()

    def __init__(self, args: Argparse) -> None:
        """Construct application attributes used as settings."""

        self.print_version: bool = args.version
        self.frozen: bool = bool(
            getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")
        )
        self.mode: str = args.mode
        self.list_programs: bool = args.list_programs
        self.list_formats: bool = args.list_formats
        self.list_examples: bool = args.list_examples
        self.path: Path = Path(sys.argv[0])
        self.programs: dict[str, Path] = {
            "Python": Path(sys.executable),
            "chdman": App.which(args.chdman),
            "7z": App.which(args.p7z),
        }
        self.output_dir: Path | None = App.existing_dir(args.output_dir)
        self.temp_path: Path | None = App.existing_dir(args.temp_dir)
        self.no_rename: bool = args.no_rename
        self.files: list[File] = self.get_files(args.file)
        if args.stdin:
            self.files.extend(self.get_files(get_stdin_lines()))
        self.chd_processors: int = args.chd_processors
        self.parallel: bool = args.parallel
        self.threads: int = args.threads
        self.quiet: bool = args.quiet
        self.names: bool = args.names
        self.dry_run: bool = args.dry_run
        self.emergency_break: bool = args.emergency_break
        self.stats: bool = args.stats
        self.stats_started: int = 0
        self.stats_skipped: int = 0
        self.stats_failed: int = 0
        self.stats_completed: int = 0

    def get_files(self, files: list[str]) -> list:
        """Filter and convert list of strings to list of supported Files."""

        new_list: list[File] = []
        for file in files:
            path: Path = fullpath(file)
            supported: File | None
            if path.is_file():
                supported = self.get_supported_file(path)
                if supported:
                    new_list.append(supported)
            elif path.is_dir():
                for dir_entry in path.iterdir():
                    supported = self.get_supported_file(dir_entry)
                    if supported:
                        new_list.append(supported)
        new_list = filter_other_in_gdi_dirs(new_list)
        new_list = filter_images_in_sheet_dirs(new_list)
        return new_list

    def get_supported_file(self, path: Path):
        """Transform Path to a File, if supported type and not hidden dir."""

        if not (self.exclude_hidden and path.name.startswith(".")):
            file: File = File(path, dir_path=self.output_dir, temp_path=self.temp_path)
            if file.type:
                return file
        return None

    def run_convert_process(self, command: list[str]) -> CompletedProcess:
        """Executes command as a process and determines stdout and stderr."""

        stdout: int | None
        stderr: int | None
        if self.quiet:
            stdout = subprocess.PIPE
            stderr = subprocess.PIPE
        elif self.parallel:
            stdout = None
            stderr = subprocess.PIPE
        else:
            stdout = None
            stderr = None
        return subprocess.run(command, stdout=stdout, stderr=stderr, text=True)

    def convert(self, file_list: list, start_index: int = 1) -> int:
        """Convert list of files to CHD."""

        last_index: int = start_index
        pool = None
        if self.parallel:
            if self.threads == 0:
                pool = multiprocessing.Pool()
            else:
                pool = multiprocessing.Pool(processes=self.threads)
        for job_index, file in enumerate(file_list, start_index):
            last_index = job_index + 1
            if self.dry_run:
                self.message_job("Skipped", file.input, job_index)
                continue
            elif file.output.exists():
                self.message_job("Skipped", file.input, job_index)
                continue
            elif file.type == "image" or file.type == "sheet":
                if self.parallel:
                    pool.apply_async(
                        self.convert_file,
                        (
                            file,
                            job_index,
                        ),
                    )
                else:
                    self.convert_file(file, job_index)
            elif file.type == "archive":
                if self.parallel:
                    pool.apply_async(
                        self.convert_archive,
                        (
                            file,
                            job_index,
                        ),
                    )
                else:
                    self.convert_archive(file, job_index)
            else:
                self.message_job("Skipped", file.input, job_index)
                continue
        if self.parallel:
            pool.close()
            pool.join()
        return last_index

    def convert_file(self, file: File, job_index: int) -> CompletedProcess:
        """Convert File to CHD executing chdman."""

        if job_index:
            self.message_job("Started", file.input, job_index)
        command: list[str] = [self.programs["chdman"].as_posix()]
        match self.mode:
            case "auto":
                if file.get_size("MB") > 750:
                    command.append("createdvd")
                else:
                    command.append("createcd")
            case "cd":
                command.append("createcd")
            case "dvd":
                command.append("createdvd")
        if self.chd_processors:
            command.append("--numprocessors")
            command.append(str(self.chd_processors))
        command.append("--input")
        command.append(file.input.as_posix())
        command.append("--output")
        command.append(file.output.as_posix())

        completed = self.run_convert_process(command)
        if job_index:
            if completed.returncode == 0 and file.output.exists():
                self.message_job("Completed", file.output, job_index)
            else:
                file.output.unlink(missing_ok=True)
                self.message_job("Failed", file.output, job_index)
        return completed

    def convert_archive(self, archive: File, job_index: int) -> CompletedProcess | None:
        """Extract and convert an archive to CHD."""

        self.message_job("Started", archive.input, job_index)
        archlist: list[File] = self.listing_from_archive(archive)
        archlist = [f for f in archlist if f.type == "image" or f.type == "sheet"]
        archlist = filter_other_in_gdi_dirs(archlist)
        archlist = filter_images_in_sheet_dirs(archlist)
        # workaround
        if [f for f in archlist if f.type == "sheet"]:
            archlist = [f for f in archlist if not f.input.suffix == ".iso"]

        if not archlist:
            self.message_job("Failed", archive.output, job_index)
            return None

        command: list[str] = [self.programs["7z"].as_posix(), "x"]
        if self.quiet or self.parallel:
            command.append("-y")
        if self.parallel:
            command.append("-bd")
        command.append(f"-o{archive.tempdir.name}")
        command.append(archive.input.as_posix())

        completed = self.run_convert_process(command)
        if completed.returncode == 0:
            for file in archlist:
                completed = self.convert_file(file, 0)
                if self.no_rename:
                    dest_path = archive.output.with_name(file.output.name)
                else:
                    dest_path = archive.output
                dest_path.unlink(missing_ok=True)
                if completed.returncode == 0:
                    shutil.move(file.output.as_posix(), dest_path.as_posix())
                    if dest_path.exists():
                        self.message_job("Completed", dest_path, job_index)
                else:
                    self.message_job("Failed", dest_path, job_index)
        else:
            self.message_job("Failed", archive.output, job_index)

        archive.tempdir.cleanup()
        return completed

    def listing_from_archive(self, file) -> list[File]:
        """Get a list of all paths in archive as File."""

        command: list[str] = [
            self.programs["7z"].as_posix(),
            "l",
            "-slt",
            "-y",
            file.input.as_posix(),
        ]

        completed = subprocess.run(command, capture_output=True, text=True)
        if completed.returncode == 0:
            listing: list[str] = re.findall(r"Path = (.+)", completed.stdout, re.M)
            return [File(file.tempdir.name / Path(entry)) for entry in listing[1:]]
        else:
            return []

    def message_job(self, message: str, path: Path, job_index: int) -> None:
        """Print job message with correct formatting."""

        if not self.parallel:
            # BUG: Counting is thread unsafe currently. Disable until a
            # solution is found.
            match message:
                case "Started":
                    self.stats_started += 1
                case "Skipped":
                    self.stats_skipped += 1
                case "Failed":
                    self.stats_failed += 1
                case "Completed":
                    self.stats_completed += 1

        pad_size: int = 12
        pad_size = pad_size - len(str(job_index))
        padded_msg = message.rjust(pad_size)
        if self.names:
            path_msg = path.name
        else:
            path_msg = path.as_posix()
        message = f"Job {job_index} {padded_msg}:\t{path_msg}"
        print(message, flush=True)
        return None

    @classmethod
    def which(cls, command: str) -> Path:
        """Find command in $PATH or get fullpath."""

        program: str | None = shutil.which(command)
        path: Path
        if program:
            path = Path(program)
        else:
            path = fullpath(command)
            if not path.exists():
                raise FileNotFoundError(path)
            elif not path.is_file():
                raise OSError("Path is not a file:", path)
            elif not os.access(path, os.X_OK):
                raise PermissionError(path)
        return path

    @classmethod
    def existing_dir(cls, path: str | None) -> Path | None:
        """Check if path is an existing dir and get fullpath."""
        if not path:
            return None

        dir_path: Path = fullpath(path)
        if not dir_path.exists():
            raise FileNotFoundError(dir_path)
        elif not dir_path.is_dir():
            raise NotADirectoryError(dir_path)
        elif not os.access(dir_path, os.W_OK):
            raise PermissionError(dir_path)
        return dir_path

    @classmethod
    def match_type(cls, path: Path) -> str | None:
        """Check and get supported file type of paths extension."""

        if path.suffix:
            suffix: str = path.suffix.lower().lstrip(".")
            for file_type, extensions in App.types.items():
                if suffix in extensions:
                    return file_type
        return None


def fullpath(file: str) -> Path:
    """Transform str to path, resolve env vars, tilde and make absolute."""

    return Path(os.path.expandvars(file)).expanduser().resolve()


def available_cpu_count() -> int:
    """Get actual number of available logical CPU count."""

    count = len(os.sched_getaffinity(0))
    if count == 0 or count is None:
        return 0
    else:
        return count


def get_stdin_lines() -> list[str]:
    """Read each line from stdin and split by newline char."""

    stdin: list[str] = []
    for line in sys.stdin.readlines():
        if line:
            stdin.extend(line.split("\n"))
    stdin_set: set[str] = set(stdin)
    stdin_set.discard("")
    return list(stdin_set)


def elapsed_time(seconds: int | float) -> str:
    elapsed = datetime.timedelta(seconds=int(seconds))
    return str(elapsed).split(".")[0]


def parse_arguments(args: list[str] | None = None) -> Argparse:
    """Programs CLI options."""

    parser = argparse.ArgumentParser(
        description="Convert game ISO and archives to CD/DVD CHD for emulation.",
        epilog=(
            "Copyright © 2022, 2024 Tuncay D. <https://github.com/thingsiplay/tochd>"
        ),
    )

    parser.add_argument(
        "file",
        default=[],
        nargs="*",
        help=(
            "input multiple files or folders containing ISOs or archive "
            "files, script will search for supported files in top level of "
            'a folder, a single dash "-" character will instruct the script '
            "to read file paths from stdin for each line, note: option "
            'double dash "--" will stop parsing for program options and '
            "everything following that is interpreted as a file"
        ),
    )

    parser.add_argument(
        "--version", default=False, action="store_true", help="print version and exit"
    )

    parser.add_argument(
        "--list-examples",
        default=False,
        action="store_true",
        help="print usage examples and exit",
    )

    parser.add_argument(
        "--list-formats",
        default=False,
        action="store_true",
        help="list all supported filetypes and extensions and exit",
    )

    parser.add_argument(
        "--list-programs",
        default=False,
        action="store_true",
        help="list name and path of all found programs and exit",
    )

    parser.add_argument(
        "--7z",
        dest="p7z",
        metavar="CMD",
        default="7z",
        help="change path or command name to 7z program",
    )

    parser.add_argument(
        "--chdman",
        metavar="CMD",
        default="chdman",
        help="change path or command name to chdman program",
    )

    parser.add_argument(
        "-d",
        "--output-dir",
        metavar="DIR",
        default=None,
        help=(
            "destination path to an existing directory to save the CHD file "
            "under, defaults to each input files' original folder"
        ),
    )

    parser.add_argument(
        "--temp-dir",
        metavar="TEMP_DIR",
        default=None,
        help=(
            "destination path to an existing directory to extract archives to, "
            "defaults to each input files' original folder"
        ),
    )

    parser.add_argument(
        "-R",
        "--no-rename",
        default=False,
        action="store_true",
        help=(
            "disable automatic renaming for CHD files that were build from "
            'archives, no test for "if file already exists" can be provided '
            "beforehand, only applicable to archive sources, without this "
            "option files from archives are renamed to match the archive"
        ),
    )

    parser.add_argument(
        "-p",
        "--parallel",
        default=False,
        action="store_true",
        help=(
            "activate multithreading to process multiple files at the same "
            "time, hides progress bar and stderr stream from invoked "
            "commands, but stdout is still output, automates user input "
            'when possible, set number of threads with option "-t"'
        ),
    )

    parser.add_argument(
        "-t",
        "--threads",
        metavar="NUM",
        default=2,
        type=int,
        choices=range(available_cpu_count() + 1),
        help=(
            "max number of threaded processes to run in parallel, requires "
            'option "-p" to be active, "0" is count of all cores '
            f'(available: {available_cpu_count()}), defaults to "2"'
        ),
    )

    parser.add_argument(
        "-c",
        "--chd-processors",
        metavar="NUM",
        default=0,
        type=int,
        choices=range(available_cpu_count() + 1),
        help=(
            "limit the number of processor cores to utilize during "
            'creation of the CHD files with "chdman" for each thread, '
            f"0 will not limit the cores (available: {available_cpu_count()}), "
            'defaults to "0"'
        ),
    )

    parser.add_argument(
        "-m",
        "--mode",
        metavar="DISC",
        default="cd",
        choices=["cd", "dvd", "auto"],
        help=(
            'disc format to create, available formats are "cd" or "dvd", '
            'or use "auto" to determine format based on filesize (750 MB '
            "threshold), some systems or emulators perform better with DVD "
            'format, defaults to "cd"'
        ),
    )

    parser.add_argument(
        "-q",
        "--quiet",
        default=False,
        action="store_true",
        help=(
            "supress output from external programs, print job messages "
            "only, automate user input when possible"
        ),
    )

    parser.add_argument(
        "-n",
        "--names",
        default=False,
        action="store_true",
        help="shorten output path as filename only for each printed job message",
    )

    parser.add_argument(
        "-s",
        "--stats",
        default=False,
        action="store_true",
        help="display additional stats, such as the elapsed time and a final summary",
    )

    parser.add_argument(
        "-E",
        "--emergency-break",
        default=False,
        action="store_true",
        help=(
            "Ctrl+c (SIGINT) will cancel all jobs and stop script execution "
            "with exit code 255, all temporary files and folders should be "
            "removed automatically, without this option script defaults to "
            "canceling current job only and move on to next"
        ),
    )

    parser.add_argument(
        "-X",
        "--dry-run",
        default=False,
        action="store_true",
        help=(
            "do not execute the conversion or extraction commands, list the "
            "jobs and files only that would have been processed"
        ),
    )

    parser.add_argument(
        "-",
        dest="stdin",
        default=False,
        action="store_true",
        help=(
            "read files from stdin for each line, additionally break up "
            'lines containing any newline character "\\n"'
        ),
    )

    if args is None:
        return parser.parse_args()
    else:
        return parser.parse_args(args)


def main(args: list[str] | None = None) -> int:
    """Run the application."""

    start_time = time.time()

    def example(arguments: str, stdin: str | None = None):
        """Template to build example line for --list-examples option."""

        if stdin:
            stdin = f"{stdin} | "
        else:
            stdin = ""
        return f"$ {stdin}{app.name} {arguments}"

    def signal_sigint(*_):
        """Shutdown clean and gracefully on Ctrl+c keyboard interruption."""

        try:
            sys.exit(3)
        except SystemExit:
            if app.emergency_break:
                sys.exit(255)

    def signal_sigterm(*_):
        """Forcefully shutdown on TERM signal. May leave unfinished files."""

        sys.exit(255)

    app: App
    if not args and not sys.argv[1:]:
        default_options: list[str] = ["-X", "."]
        print("Fallback to default options:", " ".join(default_options))
        app = App(parse_arguments(default_options))
    else:
        app = App(parse_arguments(args))

    atexit.register(signal_sigint)
    signal.signal(signal.SIGTERM, signal_sigterm)
    signal.signal(signal.SIGINT, signal_sigint)

    if app.print_version:
        if app.frozen:
            frozen = " (pyinstaller)"
        else:
            frozen = ""
        print(f"{app.name} v{app.version}{frozen}")
        return 0
    elif app.list_programs:
        for name, path in app.programs.items():
            print(name + ":", path.as_posix())
        return 0
    elif app.list_formats:
        for filetype, extension in app.types.items():
            print(filetype + ":", ", ".join(extension))
        return 0
    elif app.list_examples:
        print(example("--help"))
        print(example("-q ."))
        print(example("--quiet --stats --names ~/Downloads"))
        print(example("-p -- *.7z"))
        return 0

    if app.stats:
        print("Files in queue:", len(app.files))

    app.convert(app.files, start_index=1)

    if app.stats:
        if not app.parallel:
            # BUG: Counting variables is currently thread unsafe.
            print("Started:", app.stats_started)
            print("Skipped:", app.stats_skipped)
            print("Failed:", app.stats_failed)
            print("Completed:", app.stats_completed)
        end_time = time.time()
        print("Elapsed time:", elapsed_time(end_time - start_time))
    return 0


if __name__ == "__main__":
    sys.exit(main())
