"""Import every package module, including the command-line entry point."""

import importlib
import pkgutil

import pytest

import culprit


MODULES = [culprit.__name__] + [
    module.name for module in pkgutil.walk_packages(culprit.__path__, "culprit.")
]


@pytest.mark.parametrize("name", MODULES)
def test_import_every_module(name, monkeypatch, capsys):
    if name == "culprit.__main__":
        monkeypatch.setattr("sys.argv", ["culprit", "doctor"])
        with pytest.raises(SystemExit) as exc:
            importlib.import_module(name)
        assert exc.value.code == 1
        assert capsys.readouterr().out == "not implemented yet\n"
    else:
        importlib.import_module(name)
