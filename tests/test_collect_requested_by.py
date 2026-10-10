"""依頼元の名乗り(`requested_by`)を送り直しで変えられること。

アプリが名前を改めたとき、作ったときの名乗りのまま残ると、管理画面の依頼元と
使用量の行が前の名前のまま並ぶ。
"""
import pytest


@pytest.fixture()
def state_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CHIEZO_NOTES_DIR", str(tmp_path / "notes"))
    monkeypatch.setenv("CHIEZO_TRIGGER_URL", "http://trigger.invalid/")
    return monkeypatch


def _created(name="myapp_news"):
    from app import collect

    collect.create(
        name=name, description="ニュース", prompt="{current}\nを整理", interval_minutes=60,
        requested_by="oldname",
    )
    return name


def test_the_name_can_be_changed_by_sending_again(state_env):
    from app import collect

    name = _created()
    assert collect.get(name).requested_by == "oldname"

    collect.update(name, requested_by=" newname ")
    assert collect.get(name).requested_by == "newname"


def test_an_empty_name_does_not_erase_it(state_env):
    """送り直しで名乗りを書かない相手がいるので、空では消さない。"""
    from app import collect

    name = _created()
    collect.update(name, requested_by="", description="ニュース(改)")

    assert collect.get(name).requested_by == "oldname"
    assert collect.get(name).description == "ニュース(改)"
