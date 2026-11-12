from environment_permits.pipeline import PermitPipeline
from environment_permits.sources import FixtureSource
from environment_permits.storage import PermitStore


def test_permit_is_normalized_and_stored(tmp_path):
    html = '<meta name="company" content="测试（上海）有限公司"><meta name="permit-number" content="ep 01"><meta name="document-type" content="permit"><meta name="issued-at" content="2026-01-01">污染物：颗粒物。'
    store = PermitStore(tmp_path / "permits.sqlite3")
    try:
        document = PermitPipeline(FixtureSource(html.encode()), store).collect_one("fixture://permit")
        assert document.company_name == "测试 上海 有限公司"
        assert document.permit_number == "EP01"
        assert document.pollutants == ("颗粒物",)
        assert store.connection.execute("SELECT COUNT(*) FROM permits").fetchone()[0] == 1
    finally:
        store.close()
