import gzip
import json
from pathlib import Path

import pytest


def _record(conf, title, **overrides):
    record = {
        "conf": conf,
        "paper_name": title,
        "paper_authors": ["Alice", "Bob"],
        "paper_url": f"https://example.org/{conf}",
        "paper_abstract": f"Abstract about {title}",
        "paper_code": "#",
    }
    record.update(overrides)
    return record


def _write_corpus(path: Path, records) -> Path:
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def _provider(tmp_path, records, **config_overrides):
    from papervault_provider import PaperVaultProvider

    path = _write_corpus(tmp_path / "cache.jsonl.gz", records)
    config = {"venue_series": ["NIPS", "ICML"], "min_year": 2015}
    config.update(config_overrides)

    def downloader(repo_id, filename, repo_type):
        assert repo_id == "youngfish42/PaperVault"
        assert filename == "cache/cache.jsonl.gz"
        assert repo_type == "dataset"
        return path

    return PaperVaultProvider(config, downloader=downloader)


def test_normalize_record_maps_six_fields():
    from papervault_provider import PaperVaultProvider

    provider = PaperVaultProvider({"venue_series": ["NIPS"]})
    paper = provider.normalize_record(
        _record("NIPS2023", "Flood Forecast", paper_code="https://github.com/x/y")
    )
    assert paper["title"] == "Flood Forecast"
    assert paper["authors"] == ["Alice", "Bob"]
    assert paper["keywords"] == []
    assert paper["venue"] == "NIPS2023"
    assert paper["venue_id"] == "NIPS2023"
    assert paper["status"] == "accepted"
    assert paper["pdf"] == "https://example.org/NIPS2023"
    assert paper["forum_url"] == "https://example.org/NIPS2023"
    assert paper["code_url"] == "https://github.com/x/y"
    assert paper["cdate"] == 0 and paper["mdate"] == 0
    assert paper["invitations"] == [] and paper["camera_ready"] is False
    assert paper["id"] == paper["forum_id"] and paper["id"].startswith("pv-")


def test_normalize_record_drops_placeholder_code_and_bad_urls():
    from papervault_provider import PaperVaultProvider

    provider = PaperVaultProvider({"venue_series": ["NIPS"]})
    paper = provider.normalize_record(
        _record("NIPS2023", "T", paper_url="notaurl", paper_code="#")
    )
    assert paper["pdf"] == "" and paper["forum_url"] == "" and paper["code_url"] == ""
    paper = provider.normalize_record(
        _record("NIPS2023", "T", paper_authors="Solo Author")
    )
    assert paper["authors"] == ["Solo Author"]


def test_stable_identity_separates_confs_and_folds_title_variants():
    from papervault_provider import PaperVaultProvider

    provider = PaperVaultProvider({"venue_series": ["NIPS", "ICML"]})
    base = provider.normalize_record(_record("NIPS2023", "Flood Forecast"))
    same = provider.normalize_record(_record("NIPS2023", "flood   forecast"))
    other_conf = provider.normalize_record(_record("ICML2024", "Flood Forecast"))
    assert base["forum_id"] == same["forum_id"]
    assert base["forum_id"] != other_conf["forum_id"]


def test_config_error_without_venue_series():
    from papervault_provider import PaperVaultConfigError, PaperVaultProvider

    with pytest.raises(PaperVaultConfigError):
        PaperVaultProvider({"venue_series": []})
    with pytest.raises(PaperVaultConfigError):
        PaperVaultProvider({})


def test_discover_venue_is_config_derived_and_stable(tmp_path):
    provider = _provider(
        tmp_path, [_record("NIPS2023", "T")], venue_label="papervault:ai_conf"
    )
    caps = provider.discover_venue()
    assert caps.venue_id == "papervault:ai_conf"
    assert caps.submission_invitation == "papervault:cache/cache.jsonl.gz"
    assert caps == provider.discover_venue()

    default = _provider(tmp_path, [_record("NIPS2023", "T")])
    assert default.discover_venue().venue_id == "papervault:ICML+NIPS"


def test_fetch_forum_returns_no_replies(tmp_path):
    provider = _provider(tmp_path, [_record("NIPS2023", "T")])
    assert provider.fetch_forum("pv-x", provider.discover_venue()) == (None, [])


def test_iter_submission_pages_filters_series_and_paginates(tmp_path):
    records = [
        _record("NIPS2023", "Paper A"),
        _record("SIGGRAPH2024", "Paper excluded-series"),
        _record("NIPS2010", "Paper excluded-year"),
        _record("ICML2020", "Paper B"),
        _record("ICML2021", "Paper C"),
        _record("CVPR2022", "Paper excluded-series-2"),
    ]
    provider = _provider(tmp_path, records)
    pages = list(provider.iter_submission_pages(provider.discover_venue(), page_size=2))
    assert [len(page.papers) for page in pages] == [2, 1]
    assert [p["title"] for page in pages for p in page.papers] == [
        "Paper A",
        "Paper B",
        "Paper C",
    ]
    assert pages[0].page_number == 1 and pages[1].page_number == 2
    assert pages[0].total is None
    # raw_count counts consumed raw lines, including filtered-out ones.
    assert pages[0].raw_count == 4
    assert pages[1].raw_count == 2
    # Cursor lands on the final raw line of the file.
    assert pages[-1].cursor_after == "6"


def test_iter_submission_pages_skips_malformed_lines(tmp_path):
    path = tmp_path / "cache.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write(json.dumps(_record("NIPS2023", "Good Paper")) + "\n")
        handle.write("{not json\n")
        handle.write(json.dumps(_record("ICML2020", "Also Good")) + "\n")

    from papervault_provider import PaperVaultProvider

    provider = PaperVaultProvider(
        {"venue_series": ["NIPS", "ICML"]},
        downloader=lambda *args: path,
    )
    pages = list(provider.iter_submission_pages(provider.discover_venue()))
    assert [p["title"] for page in pages for p in page.papers] == [
        "Good Paper",
        "Also Good",
    ]
    assert pages[-1].cursor_after == "3"


def test_iter_submission_pages_resumes_from_line_cursor(tmp_path, monkeypatch):
    provider = _provider(
        tmp_path,
        [
            _record("NIPS2023", "Paper A"),
            _record("ICML2020", "Paper B"),
            _record("ICML2021", "Paper C"),
        ],
    )
    caps = provider.discover_venue()
    first = list(provider.iter_submission_pages(caps, page_size=2))
    assert [p["title"] for page in first for p in page.papers] == [
        "Paper A",
        "Paper B",
        "Paper C",
    ]
    cursor = first[0].cursor_after

    import papervault_provider

    calls = {"count": 0}
    real_loads = json.loads

    def counting_loads(value):
        calls["count"] += 1
        return real_loads(value)

    monkeypatch.setattr(papervault_provider.json, "loads", counting_loads)
    resumed = list(provider.iter_submission_pages(caps, after_id=cursor, page_size=2))
    assert [p["title"] for page in resumed for p in page.papers] == ["Paper C"]
    # Only the single line after the cursor is parsed; skipped lines are not.
    assert calls["count"] == 1


def test_downloader_injection_avoids_huggingface_import(monkeypatch):
    import builtins

    from papervault_provider import PaperVaultProvider

    real_import = builtins.__import__

    def deny_import(name, *args, **kwargs):
        if name == "huggingface_hub":
            raise AssertionError("huggingface_hub must not be imported")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", deny_import)
    provider = PaperVaultProvider({"venue_series": ["NIPS"]})
    assert provider._downloader is PaperVaultProvider._default_downloader
    monkeypatch.undo()
