import importlib.metadata
from pathlib import Path

import pytest

EXPECTED_URLS = {
    "Homepage": "https://github.com/HafidIdrissi/github-traffic-archive",
    "Repository": "https://github.com/HafidIdrissi/github-traffic-archive",
    "Issues": "https://github.com/HafidIdrissi/github-traffic-archive/issues",
    "Changelog": "https://github.com/HafidIdrissi/github-traffic-archive/blob/main/CHANGELOG.md",
}


def test_package_metadata_project_urls():
    """Verify built package metadata includes Homepage, Repository, Issues, and Changelog Project-URL fields."""
    try:
        meta = importlib.metadata.metadata("github-traffic-archive")
    except importlib.metadata.PackageNotFoundError:
        pytest.fail(
            "Package 'github-traffic-archive' is not installed in the current environment. "
            "Run 'python -m pip install --no-deps .' or 'python -m pip install -e .' to build and install metadata."
        )

    urls = meta.get_all("Project-URL") or []
    found_urls = {}
    for entry in urls:
        if ", " in entry:
            label, url = entry.split(", ", 1)
            found_urls[label.strip()] = url.strip()
        elif "," in entry:
            label, url = entry.split(",", 1)
            found_urls[label.strip()] = url.strip()

    for label, expected_url in EXPECTED_URLS.items():
        assert label in found_urls, f"Missing Project-URL label: {label}"
        assert found_urls[label] == expected_url, (
            f"Expected Project-URL for '{label}' to be '{expected_url}', got '{found_urls.get(label)}'"
        )


def test_pyproject_source_project_urls():
    """Verify pyproject.toml source file explicitly declares the matching project.urls table."""
    pyproject_path = Path(__file__).resolve().parent.parent / "pyproject.toml"
    assert pyproject_path.exists(), "pyproject.toml file not found"
    content = pyproject_path.read_text(encoding="utf-8")

    try:
        import tomllib

        data = tomllib.loads(content)
        parsed_urls = data.get("project", {}).get("urls", {})
        assert parsed_urls == EXPECTED_URLS
    except ImportError:
        for label, expected_url in EXPECTED_URLS.items():
            assert f'{label} = "{expected_url}"' in content
