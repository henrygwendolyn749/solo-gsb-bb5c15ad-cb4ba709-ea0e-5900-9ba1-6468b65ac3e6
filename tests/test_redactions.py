"""快照敏感词遮盖测试。

覆盖：
- 基本遮盖：命中码点逐个替换为 *，未命中文字原样保留；
- 相邻字幕按序号无分隔拼接（跨序号词语完整遮盖），缺口阻断命中；
- 区分大小写、逐码点精确匹配、不做 Unicode 归一化；
- 多词重叠命中取覆盖区间并集；
- 遮盖基于快照冻结的生效文本（快照后的更正不影响结果）；
- 只读：遮盖不修改快照条目与当前字幕；
- 先按完整快照计算再分页：跨页词语完整遮盖，next_after 有后续时
  指向本页末序号、否则为 null；
- 词表去重后 1~20 项、每项 1~128 码点，after/limit 越界等 -> 422；
- 快照不存在 -> 404。
"""


def submit(client, channel, seq, revision, text="t"):
    resp = client.post(
        f"/channels/{channel}/fragments",
        json={"seq": seq, "revision": revision, "text": text},
    )
    assert resp.status_code == 200
    return resp.json()


def decide_gap(client, channel, start_seq, end_seq):
    resp = client.post(
        f"/channels/{channel}/gaps",
        json={"start_seq": start_seq, "end_seq": end_seq},
    )
    assert resp.status_code == 200
    return resp.json()


def correct(client, channel, seq, expected_version, text, reason="r"):
    resp = client.post(
        f"/channels/{channel}/finalized/{seq}/corrections",
        json={
            "expected_version": expected_version,
            "text": text,
            "reason": reason,
        },
    )
    assert resp.status_code == 200
    return resp.json()


def make_snapshot(client, channel, key):
    resp = client.post(
        f"/channels/{channel}/snapshots", json={"idempotency_key": key}
    )
    assert resp.status_code == 200
    return resp.json()


def redact(client, snapshot_id, words, **params):
    body = {"words": words}
    body.update(params)
    return client.post(f"/snapshots/{snapshot_id}/redactions", json=body)


# ---------- 基本遮盖语义 ----------


def test_basic_redaction_replaces_each_covered_codepoint(client):
    submit(client, "c1", 1, 1, "各位观众晚上好")
    submit(client, "c1", 2, 1, "今天没有命中词")
    snap = make_snapshot(client, "c1", "k1")

    resp = redact(client, snap["snapshot_id"], ["观众"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["snapshot_id"] == snap["snapshot_id"]
    assert body["channel"] == "c1"
    assert body["watermark"] == 2
    assert body["words"] == ["观众"]
    assert body["next_after"] is None
    items = body["items"]
    assert [i["seq"] for i in items] == [1, 2]
    # 每个被覆盖码点恰好替换为一个 *
    assert items[0]["text"] == "各位**晚上好"
    assert items[0]["kind"] == "subtitle"
    assert items[0]["revision"] == 1
    # 未命中文字原样保留
    assert items[1]["text"] == "今天没有命中词"


def test_multiple_words_and_overlapping_hits_union(client):
    submit(client, "c1", 1, 1, "aaaa")
    submit(client, "c1", 2, 1, "abcdef")
    snap = make_snapshot(client, "c1", "k1")

    # 重叠命中取并集：aa 命中 [0,2) [1,3) [2,4)，aaa 命中 [0,3) [1,4) -> 全覆盖
    body = redact(client, snap["snapshot_id"], ["aa", "aaa"]).json()
    assert body["items"][0]["text"] == "****"
    # 多词各自命中：bcd 覆盖 1..3，f 覆盖 5
    body = redact(client, snap["snapshot_id"], ["bcd", "f"]).json()
    assert body["items"][1]["text"] == "a***e*"


def test_case_sensitive_and_no_normalization(client):
    # 组合字符：é(U+00E9) 与 e+́(U+0065 U+0301) 是不同码点序列
    submit(client, "c1", 1, 1, "AbC é é")
    snap = make_snapshot(client, "c1", "k1")

    # 区分大小写：abc 不命中 AbC
    body = redact(client, snap["snapshot_id"], ["abc"]).json()
    assert body["items"][0]["text"] == "AbC é é"
    body = redact(client, snap["snapshot_id"], ["AbC"]).json()
    assert body["items"][0]["text"] == "*** é é"

    # 不做归一化：单码点 é 不命中双码点 e+́，反之亦然
    body = redact(client, snap["snapshot_id"], ["é"]).json()
    assert body["items"][0]["text"] == "AbC * é"
    body = redact(client, snap["snapshot_id"], ["é"]).json()
    assert body["items"][0]["text"] == "AbC é **"


def test_adjacent_subtitles_concatenated_without_separator(client):
    submit(client, "c1", 1, 1, "今天天气")
    submit(client, "c1", 2, 1, "真的很好")
    submit(client, "c1", 3, 1, "适合出门")
    snap = make_snapshot(client, "c1", "k1")

    # “气真”跨序号 1-2 边界，“好适”跨序号 2-3 边界
    body = redact(client, snap["snapshot_id"], ["气真", "好适"]).json()
    assert [i["text"] for i in body["items"]] == [
        "今天天*",
        "*的很*",
        "*合出门",
    ]


def test_gap_blocks_matches_across_it(client):
    submit(client, "c1", 1, 1, "ab")
    submit(client, "c1", 3, 1, "cd")
    decide_gap(client, "c1", 2, 2)
    snap = make_snapshot(client, "c1", "k1")

    # 缺口阻断：bc 跨缺口永不命中；b / c 各自仍可在段内命中
    body = redact(client, snap["snapshot_id"], ["bc"]).json()
    assert [i["text"] for i in body["items"]] == ["ab", None, "cd"]

    body = redact(client, snap["snapshot_id"], ["b", "c"]).json()
    items = body["items"]
    assert items[0]["text"] == "a*"
    # 缺口标记原样保留
    assert items[1] == {
        "kind": "gap",
        "seq": 2,
        "revision": None,
        "text": None,
        "start_seq": 2,
        "end_seq": 2,
    }
    assert items[2]["text"] == "*d"


def test_unicode_words_match_by_codepoint(client):
    submit(client, "c1", 1, 1, "直播🎉开始🎉啦")
    snap = make_snapshot(client, "c1", "k1")

    body = redact(client, snap["snapshot_id"], ["🎉开始🎉"]).json()
    assert body["items"][0]["text"] == "直播****啦"


# ---------- 基于快照冻结的生效文本 ----------


def test_redaction_uses_frozen_effective_text(client):
    submit(client, "c1", 1, 1, "原始秘密文本")
    correct(client, "c1", 1, 0, "更正公开文本")
    snap = make_snapshot(client, "c1", "k1")
    # 快照后再更正：不影响既有快照的遮盖
    correct(client, "c1", 1, 1, "快照之后再改")

    sid = snap["snapshot_id"]
    # 生效文本是快照时刻的“更正公开文本”
    body = redact(client, sid, ["公开"]).json()
    assert body["items"][0]["text"] == "更正**文本"
    # 原始文本里的词不命中（遮盖基于生效文本而非原始定稿）
    body = redact(client, sid, ["秘密"]).json()
    assert body["items"][0]["text"] == "更正公开文本"
    # 快照后更正里的词同样不命中
    body = redact(client, sid, ["再改"]).json()
    assert body["items"][0]["text"] == "更正公开文本"


def test_redaction_is_read_only(client):
    submit(client, "c1", 1, 1, "秘密内容")
    snap = make_snapshot(client, "c1", "k1")
    sid = snap["snapshot_id"]

    body = redact(client, sid, ["秘密"]).json()
    assert body["items"][0]["text"] == "**内容"

    # 快照条目未被修改
    frozen = client.get(
        f"/snapshots/{sid}/items", params={"view": "effective"}
    ).json()
    assert frozen["items"][0]["text"] == "秘密内容"
    # 当前字幕未被修改
    live = client.get("/channels/c1/finalized").json()
    assert live["items"][0]["text"] == "秘密内容"


# ---------- 分页：先完整计算再截取 ----------


def test_pagination_and_next_after(client):
    for seq in range(1, 6):
        submit(client, "c1", seq, 1, f"第{seq}条")
    snap = make_snapshot(client, "c1", "k1")
    sid = snap["snapshot_id"]

    body = redact(client, sid, ["不存在"], after=0, limit=2).json()
    assert [i["seq"] for i in body["items"]] == [1, 2]
    assert body["next_after"] == 2

    body = redact(client, sid, ["不存在"], after=2, limit=2).json()
    assert [i["seq"] for i in body["items"]] == [3, 4]
    assert body["next_after"] == 4

    # 末页：无后续条目时 next_after 为 null
    body = redact(client, sid, ["不存在"], after=4, limit=2).json()
    assert [i["seq"] for i in body["items"]] == [5]
    assert body["next_after"] is None

    # after 越过水位线：空页
    body = redact(client, sid, ["不存在"], after=5).json()
    assert body["items"] == []
    assert body["next_after"] is None
    assert body["watermark"] == 5

    # 逐页拉全且与整页结果一致
    seen, after = [], 0
    while True:
        page = redact(client, sid, ["第"], after=after, limit=2).json()
        seen.extend(page["items"])
        if page["next_after"] is None:
            break
        after = page["next_after"]
    assert [i["seq"] for i in seen] == [1, 2, 3, 4, 5]
    assert all(i["text"].startswith("*") for i in seen)


def test_cross_page_word_is_fully_redacted(client):
    submit(client, "c1", 1, 1, "ab")
    submit(client, "c1", 2, 1, "cd")
    snap = make_snapshot(client, "c1", "k1")
    sid = snap["snapshot_id"]

    # 词 bc 跨页边界：第一页只含 seq 1，仍按完整快照遮盖
    page1 = redact(client, sid, ["bc"], after=0, limit=1).json()
    assert page1["items"][0]["text"] == "a*"
    assert page1["next_after"] == 1
    page2 = redact(client, sid, ["bc"], after=1, limit=1).json()
    assert page2["items"][0]["text"] == "*d"
    assert page2["next_after"] is None


def test_empty_snapshot_returns_empty_page(client):
    snap = make_snapshot(client, "ghost", "k1")
    body = redact(client, snap["snapshot_id"], ["词"]).json()
    assert body["watermark"] == 0
    assert body["items"] == []
    assert body["next_after"] is None


# ---------- 参数校验（422）与 404 ----------


def test_words_validation(client):
    submit(client, "c1", 1, 1, "text")
    snap = make_snapshot(client, "c1", "k1")
    sid = snap["snapshot_id"]

    # 缺 words / 空表 / 去重后超 20 项
    assert client.post(f"/snapshots/{sid}/redactions", json={}).status_code == 422
    assert redact(client, sid, []).status_code == 422
    assert redact(client, sid, [f"w{i}" for i in range(21)]).status_code == 422
    # 空词 / 超 128 码点
    assert redact(client, sid, [""]).status_code == 422
    assert redact(client, sid, ["x" * 129]).status_code == 422
    assert redact(client, sid, ["ok", ""]).status_code == 422
    # 非字符串词
    assert redact(client, sid, [123]).status_code == 422
    # after / limit 越界
    assert redact(client, sid, ["w"], after=-1).status_code == 422
    assert redact(client, sid, ["w"], limit=0).status_code == 422
    assert redact(client, sid, ["w"], limit=101).status_code == 422

    # 边界合法：去重后 20 项、128 码点词、limit=100
    assert redact(client, sid, [f"w{i}" for i in range(20)]).status_code == 200
    assert redact(client, sid, ["x" * 128]).status_code == 200
    assert redact(client, sid, ["w"], after=0, limit=100).status_code == 200


def test_words_are_deduplicated_before_count_check(client):
    submit(client, "c1", 1, 1, "abc abc")
    snap = make_snapshot(client, "c1", "k1")
    sid = snap["snapshot_id"]

    # 25 个原始项去重后仅 2 项：合法；响应 words 保持首次出现顺序
    resp = redact(client, sid, ["abc", "b"] * 10 + ["abc"] * 5)
    assert resp.status_code == 200
    body = resp.json()
    assert body["words"] == ["abc", "b"]
    # 空格未被任何词覆盖，原样保留
    assert body["items"][0]["text"] == "*** ***"


def test_unknown_snapshot_returns_404(client):
    resp = redact(client, "no-such-snapshot", ["词"])
    assert resp.status_code == 404
    assert resp.json()["error"] == "snapshot_not_found"
