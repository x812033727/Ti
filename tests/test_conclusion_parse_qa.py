"""QA 驗證：flow.parse_conclusion 純函式（任務 #1 驗收 #1）。

涵蓋：四前綴正常分類／全形冒號容錯／多行同前綴／空內容跳過／
四前綴全缺回空骨架不拋例外／非字串/None 防呆。
"""

from studio.flow import parse_conclusion

EMPTY = {"consensus": [], "disagreements": [], "open_questions": [], "actions": []}


def test_四前綴正常分類():
    text = """共識: 採用混合範式
分歧: 演算法選型有爭議
未決: 上線時程未定
行動: 補測試覆蓋"""
    r = parse_conclusion(text)
    assert r["consensus"] == ["採用混合範式"]
    assert r["disagreements"] == ["演算法選型有爭議"]
    assert r["open_questions"] == ["上線時程未定"]
    assert r["actions"] == ["補測試覆蓋"]


def test_全形冒號容錯():
    text = "共識：全形冒號也能解\n行動：執行"
    r = parse_conclusion(text)
    assert r["consensus"] == ["全形冒號也能解"]
    assert r["actions"] == ["執行"]


def test_前綴前後空白容錯():
    text = "   共識  :  帶縮排與空白  "
    r = parse_conclusion(text)
    assert r["consensus"] == ["帶縮排與空白"]


def test_同前綴多行各自收集():
    text = "共識: A\n共識: B\n分歧: C"
    r = parse_conclusion(text)
    assert r["consensus"] == ["A", "B"]
    assert r["disagreements"] == ["C"]


def test_空內容前綴被跳過():
    # 「共識:」後無內容 → 不產生空字串項
    text = "共識:   \n共識: 真內容"
    r = parse_conclusion(text)
    assert r["consensus"] == ["真內容"]


def test_四前綴全缺回空骨架不拋例外():
    r = parse_conclusion("這是一段沒有任何前綴的自由文字\n第二行也沒有")
    assert r == EMPTY


def test_空字串回空骨架():
    assert parse_conclusion("") == EMPTY


def test_None_輸入不拋例外():
    # text or "" 應吸收 None
    assert parse_conclusion(None) == EMPTY


def test_鍵齊全且皆為list():
    r = parse_conclusion("亂打")
    assert set(r.keys()) == {"consensus", "disagreements", "open_questions", "actions"}
    assert all(isinstance(v, list) for v in r.values())


def test_部分前綴缺失其餘正常():
    # 只有共識與行動，分歧/未決應為空 list 而非缺鍵
    text = "共識: 有\n行動: 做"
    r = parse_conclusion(text)
    assert r["consensus"] == ["有"]
    assert r["actions"] == ["做"]
    assert r["disagreements"] == []
    assert r["open_questions"] == []


def test_自證對應_內容回指輸入():
    # 排除假綠：輸出必須回指本次獨特輸入字串
    token = "錨點驗證X9Z"
    r = parse_conclusion(f"共識: {token}")
    assert r["consensus"] == [token]


def test_行內前綴不誤觸發():
    # 「共識」出現在行中段（非行首前綴）不應被解析
    text = "我們對共識: 這不是前綴"
    r = parse_conclusion(text)
    # ^\s*共識 行首錨定 → 此行不匹配
    assert r["consensus"] == []
