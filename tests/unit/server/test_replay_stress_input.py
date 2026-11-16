"""Keep the scale fixture reproducible without running the long benchmark in unit tests."""

import sqlite3

from scripts.generate_replay_stress import generate


def test_generated_interleaved_source_has_exact_identity_and_proof_cardinalities(tmp_path):
    report = generate(tmp_path, 7, "stress", 32, 4, True, 4)
    assert report["sequence"] == 142
    with sqlite3.connect(tmp_path / "account-7/.replay-v2.sqlite") as db:
        counts = dict(db.execute("SELECT kind,count(*) FROM objects WHERE first_seq IS NOT NULL GROUP BY kind"))
        assert counts == {"command": 32, "execution": 1, "order": 32, "position": 4, "trade": 32}
        assert db.execute("SELECT count(*) FROM proof_members").fetchone()[0] == 64
        assert (
            db.execute(
                "SELECT count(*) FROM object_refs r JOIN objects o ON o.id=r.object WHERE o.kind='trade'"
            ).fetchone()[0]
            == 64
        )
        assert db.execute("SELECT seq FROM recordings").fetchone()[0] == report["sequence"]
