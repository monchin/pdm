from __future__ import annotations

import abc
import os
import shutil
import subprocess
import sys
from functools import cached_property
from pathlib import Path
from typing import Any, Iterable, Mapping

from pdm import termui
from pdm.cli.commands.venv.utils import get_venv_prefix
from pdm.exceptions import PdmUsageError, ProjectError
from pdm.models.python import PythonInfo
from pdm.project import Project


class VirtualenvCreateError(ProjectError):
    pass


class Backend(abc.ABC):
    """The base class for virtualenv backends"""

    def __init__(self, project: Project, python: str | None) -> None:
        self.project = project
        self.python = python

    @abc.abstractmethod
    def pip_args(self, with_pip: bool) -> Iterable[str]:
        pass

    @cached_property
    def _resolved_interpreter(self) -> PythonInfo:
        if not self.python:
            project_python = self.project._python
            if project_python:
                return project_python

        def match_func(py_version: PythonInfo) -> bool:
            return bool(self.python) or (
                py_version.valid and self.project.python_requires.contains(py_version.version, True)
            )

        respect_version_file = self.project.config["python.use_python_version"]
        for py_version in self.project.iter_interpreters(
            self.python, search_venv=False, filter_func=match_func, respect_version_file=respect_version_file
        ):
            return py_version

        python = f" {self.python}" if self.python else ""
        raise VirtualenvCreateError(f"Can't resolve python interpreter{python}")

    @property
    def ident(self) -> str:
        """Get the identifier of this virtualenv.
        self.python can be one of:
            3.8
            /usr/bin/python
            3.9.0a4
            python3.8
        """
        return self._resolved_interpreter.identifier

    def subprocess_call(self, cmd: list[str], **kwargs: Any) -> None:
        self.project.core.ui.echo(
            f"Run command: [success]{cmd}[/]",
            verbosity=termui.Verbosity.DETAIL,
            err=True,
        )
        try:
            subprocess.check_call(
                cmd,
                stdout=subprocess.DEVNULL if self.project.core.ui.verbosity < termui.Verbosity.DETAIL else None,
            )
        except subprocess.CalledProcessError as e:  # pragma: no cover
            raise VirtualenvCreateError(e) from None

    def _ensure_clean(self, location: Path, force: bool = False) -> None:
        if not location.exists():
            return
        if location.is_dir() and not any(location.iterdir()):
            return
        if not force:
            raise VirtualenvCreateError(f"The location {location} is not empty, add --force to overwrite it.")
        if location.is_file():
            self.project.core.ui.info(f"Removing existing file {location}", verbosity=termui.Verbosity.DETAIL)
            location.unlink()
        else:
            self.project.core.ui.info(
                f"Cleaning existing target directory {location}", verbosity=termui.Verbosity.DETAIL
            )
            with os.scandir(location) as entries:
                for entry in entries:
                    if entry.is_dir() and not entry.is_symlink():
                        shutil.rmtree(entry.path)
                    else:
                        os.remove(entry.path)

    def get_location(self, name: str | None = None, venv_name: str | None = None) -> Path:
        if name and venv_name:
            raise PdmUsageError("Cannot specify both name and venv_name")
        venv_parent = Path(self.project.config["venv.location"]).expanduser()
        if not venv_parent.is_dir():
            venv_parent.mkdir(exist_ok=True, parents=True)
        if not venv_name:
            venv_name = f"{get_venv_prefix(self.project)}{name or self.ident}"
        return venv_parent / venv_name

    def create(
        self,
        name: str | None = None,
        args: tuple[str, ...] = (),
        force: bool = False,
        in_project: bool = False,
        prompt: str | None = None,
        with_pip: bool = False,
        venv_name: str | None = None,
    ) -> Path:
        if in_project:
            location = self.project.root / ".venv"
        else:
            location = self.get_location(name, venv_name)
        args = (*self.pip_args(with_pip), *args)
        if prompt is not None:
            prompt = prompt.format(
                project_name=self.project.root.name.lower() or "virtualenv",
                python_version=self.ident,
            )
        self._ensure_clean(location, force)
        self.perform_create(location, args, prompt=prompt)
        return location

    @abc.abstractmethod
    def perform_create(self, location: Path, args: tuple[str, ...], prompt: str | None = None) -> None:
        pass


class VirtualenvBackend(Backend):
    def pip_args(self, with_pip: bool) -> Iterable[str]:
        if with_pip:
            return ()
        return ("--no-pip", "--no-setuptools", "--no-wheel")

    def perform_create(self, location: Path, args: tuple[str, ...], prompt: str | None = None) -> None:
        prompt_option = (f"--prompt={prompt}",) if prompt else ()
        cmd = [
            sys.executable,
            "-m",
            "virtualenv",
            str(location),
            "-p",
            str(self._resolved_interpreter.executable),
            *prompt_option,
            *args,
        ]
        self.subprocess_call(cmd)


class VenvBackend(VirtualenvBackend):
    def pip_args(self, with_pip: bool) -> Iterable[str]:
        if with_pip:
            return ()
        return ("--without-pip",)

    def perform_create(self, location: Path, args: tuple[str, ...], prompt: str | None = None) -> None:
        prompt_option = (f"--prompt={prompt}",) if prompt else ()
        cmd = [str(self._resolved_interpreter.executable), "-m", "venv", str(location), *prompt_option, *args]
        self.subprocess_call(cmd)


class UvBackend(VirtualenvBackend):
    def pip_args(self, with_pip: bool) -> Iterable[str]:
        if with_pip:
            return ("--seed",)
        return ()

    def perform_create(self, location: Path, args: tuple[str, ...], prompt: str | None = None) -> None:
        prompt_option = (f"--prompt={prompt}",) if prompt else ()
        cmd = [
            *self.project.core.uv_cmd,
            "venv",
            "-p",
            str(self._resolved_interpreter.executable),
            *prompt_option,
            *args,
            str(location),
        ]
        self.subprocess_call(cmd)


class CondaBackend(Backend):
    @property
    def ident(self) -> str:
        # Conda supports specifying python that doesn't exist,
        # use the passed-in name directly
        if self.python:
            return self.python
        return super().ident

    def pip_args(self, with_pip: bool) -> Iterable[str]:
        if with_pip:
            return ("pip",)
        return ()

    def perform_create(self, location: Path, args: tuple[str, ...], prompt: str | None = None) -> None:
        if self.python:
            python_ver = self.python
        else:
            python = self._resolved_interpreter
            python_ver = f"{python.major}.{python.minor}"
        if any(arg.startswith("python=") for arg in args):
            raise PdmUsageError("Cannot use python= in conda creation arguments")

        cmd = ["conda", "create", "--yes", "--prefix", str(location), f"python={python_ver}", *args]
        self.subprocess_call(cmd)


BACKENDS: Mapping[str, type[Backend]] = {
    "virtualenv": VirtualenvBackend,
    "venv": VenvBackend,
    "conda": CondaBackend,
    "uv": UvBackend,
}
