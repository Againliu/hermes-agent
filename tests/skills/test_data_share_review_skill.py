"""Behaviour contracts for optional-skills/security/data-share-review/scripts/data_share.py.

The script's promise is that it reports counts and metadata and never echoes a
value from the file it inspects, while still catching identifiers that survive
a column-level transform (an address quoted in a notes field).
"""
import csv
import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = (Path(__file__).resolve().parents[2]
          / "optional-skills/security/data-share-review/scripts/data_share.py")
EMAIL = "jane.doe@example.com"
KEY_SHAPED = "sk-" + "a1b2c3d4e5" * 3


@pytest.fixture(scope="module")
def ds():
    return SimpleNamespace(**runpy.run_path(str(SCRIPT)))


def _write_csv(path: Path, rows: list[dict]) -> Path:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


def _run(ds, capsys, *argv) -> tuple[int, dict, str]:
    code = ds.main(list(argv))
    out = capsys.readouterr().out
    return code, json.loads(out), out


@pytest.fixture
def export(tmp_path):
    return _write_csv(tmp_path / "export.csv", [
        {"customer_id": "C-10001", "email": EMAIL, "postcode": "94110",
         "notes": f"customer wrote from {EMAIL} and pasted api_key={KEY_SHAPED}"},
        {"customer_id": "C-10002", "email": "bob@example.org", "postcode": "94110", "notes": "no contact"},
    ])


def test_pseudonymized_column_still_leaks_through_free_text_and_scan_never_echoes_it(ds, capsys, tmp_path, export):
    out_dir = tmp_path / "share"
    out_dir.mkdir()
    shipped = out_dir / "share.csv"
    code, _, _ = _run(ds, capsys, "pseudonymize", str(export), str(shipped),
                      "--columns", "customer_id,email", "--key-file", str(tmp_path / "keys" / "vendor.key"))
    assert code == 0
    assert EMAIL not in next(csv.DictReader(shipped.open(encoding="utf-8")))["email"]

    code, report, raw = _run(ds, capsys, "scan", str(shipped), "--originals", str(export),
                             "--columns", "email,customer_id")
    assert code == 0
    assert report["original_values"] == {"email": 1, "customer_id": 0}
    assert report["totals"]["email"] == 1 and report["totals"]["secret"] == 1
    assert report["per_column"] == {"notes": {"email": 1, "secret": 1}}
    assert report["all_zero"] is False
    assert EMAIL not in raw and KEY_SHAPED not in raw


def test_pseudonyms_are_stable_per_key_and_differ_across_keys(ds, capsys, tmp_path, export):
    (tmp_path / "out").mkdir()

    def pseudonyms(key_name, out_name):
        out = tmp_path / "out" / out_name
        _run(ds, capsys, "pseudonymize", str(export), str(out), "--columns", "email",
             "--drop", "notes", "--key-file", str(tmp_path / "keys" / key_name))
        rows = list(csv.DictReader(out.open(encoding="utf-8")))
        assert "notes" not in rows[0]
        return [r["email"] for r in rows]

    first, again, other = pseudonyms("a.key", "1.csv"), pseudonyms("a.key", "2.csv"), pseudonyms("b.key", "3.csv")
    assert first == again
    assert first != other
    assert all(p.startswith("p_") for p in first)


def test_key_inside_the_shipped_folder_is_refused_before_anything_is_written(ds, capsys, tmp_path, export):
    out_dir = tmp_path / "share"
    out_dir.mkdir()
    code, report, _ = _run(ds, capsys, "pseudonymize", str(export), str(out_dir / "share.csv"),
                           "--columns", "email", "--key-file", str(out_dir / "nested" / "k.key"))
    assert code == 2 and "outside" in report["error"]
    assert list(out_dir.iterdir()) == []


def test_clean_file_reports_all_zero_and_k_anonymity_groups(ds, capsys, tmp_path):
    clean = _write_csv(tmp_path / "clean.csv", [
        {"region": "west", "plan": "pro", "orders": "3"},
        {"region": "west", "plan": "pro", "orders": "5"},
        {"region": "east", "plan": "free", "orders": "1"},
    ])
    _, report, _ = _run(ds, capsys, "scan", str(clean))
    assert report["all_zero"] is True
    _, report, _ = _run(ds, capsys, "scan", str(clean), "--quasi", "region,plan", "--k", "2")
    assert report["quasi_identifier"]["rows_in_groups_below_k"] == 1
    assert report["all_zero"] is False


def test_parse_error_reports_only_its_type(ds, capsys, tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text(f'{{"email": "{EMAIL}"}}\n{{"email": "{EMAIL}", oops}}\n', encoding="utf-8")
    code, report, raw = _run(ds, capsys, "scan", str(bad))
    assert code == 2 and report == {"error": "JSONDecodeError"}
    assert EMAIL not in raw
