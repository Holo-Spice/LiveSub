"""Build the small, explicit CLI payload embedded in the launcher EXE."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


root = Path(__file__).resolve().parents[1]
destination = root / "launcher" / "Resources" / "core-package.zip"
files = {
    "pyproject.toml": "pyproject.toml",
    "requirements.lock": "requirements.lock",
    "config.toml": "config.toml.template",
    "README.md": "README.md",
    "THIRD_PARTY_NOTICES.md": "THIRD_PARTY_NOTICES.md",
    "src/subtitle_cli/__init__.py": "src/subtitle_cli/__init__.py",
    "src/subtitle_cli/audio.py": "src/subtitle_cli/audio.py",
    "src/subtitle_cli/cli.py": "src/subtitle_cli/cli.py",
    "src/subtitle_cli/models.py": "src/subtitle_cli/models.py",
    "src/subtitle_cli/pipeline.py": "src/subtitle_cli/pipeline.py",
    "src/subtitle_cli/subtitles.py": "src/subtitle_cli/subtitles.py",
    "src/subtitle_cli/translator.py": "src/subtitle_cli/translator.py",
    "src/subtitle_cli/ui_bridge.py": "src/subtitle_cli/ui_bridge.py",
    "testdata/calibration-two-sentences-pts2s.mkv": "testdata/probe-en.mkv",
}

destination.parent.mkdir(parents=True, exist_ok=True)
with ZipFile(destination, "w", ZIP_DEFLATED, compresslevel=9) as archive:
    for source, target in files.items():
        path = root / source
        if not path.is_file():
            raise FileNotFoundError(path)
        archive.write(path, target)
print(destination)
