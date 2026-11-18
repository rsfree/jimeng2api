from app.upstream import protocol
from app.upstream.sign import SELF_TEST_VECTORS, sign_headers, verify_vectors


def test_sign_vectors_all_match():
    assert verify_vectors() == []


def test_sign_only_depends_on_path_tail_and_ts():
    a = sign_headers("/mweb/v1/aigc_draft/generate", ts=1789742348)["sign"]
    b = sign_headers("/x/y/z/enerate", ts=1789742348)["sign"]
    assert a == b  # 只看路径末 7 字符
    assert sign_headers("/mweb/v1/aigc_draft/generate", ts=1789742349)["sign"] != a


def test_device_time_matches_sign_second():
    h = sign_headers("/mweb/v1/get_common_config", ts=1795014070)
    assert h["device-time"] == "1795014070"
    assert h["sign"] == "192451e631689e8e07955c9525b86467"
    assert len(SELF_TEST_VECTORS) >= 6


def test_parse_size_variants():
    assert protocol.parse_size("2048x2048") == (2048, 2048)
    assert protocol.parse_size("2048*1536") == (2048, 1536)
    assert protocol.parse_size("1024X1024") == (1024, 1024)


def test_ratio_and_resolution_snap():
    assert protocol.ratio_for_size(2048, 2048) == 1
    assert protocol.ratio_for_size(2560, 1440) == 3
    assert protocol.ratio_for_size(1440, 2560) == 5
    assert protocol.resolution_type_for_size(1536, 1536) == "1.5k"
    assert protocol.resolution_type_for_size(2048, 2048) == "2k"
    assert protocol.resolution_type_for_size(4096, 4096) == "4k"


def test_draft_is_double_encoded_json_string():
    draft = protocol.build_t2i_draft(
        prompt="猫", model="high_aes_general_v55", count=2,
        width=2048, height=2048, da_version="3.3.40")
    assert isinstance(draft, str)
    parsed = __import__("json").loads(draft)
    assert parsed["type"] == "draft"
    comp = parsed["component_list"][0]
    assert comp["abilities"]["gen_option"]["gen_count"] == 2
    assert comp["abilities"]["generate"]["core_param"]["image_ratio"] == 1


def test_blend_draft_has_no_metadata_and_counts():
    draft = protocol.build_blend_draft(
        prompt="改成霓虹", image_uris=["tos-cn-i-x/abc"],
        model="high_aes_general_v55", count=1, width=2048, height=2048,
        da_version="3.3.40")
    parsed = __import__("json").loads(draft)
    comp = parsed["component_list"][0]
    assert comp["gen_type"] == 12
    assert "metadata" not in comp
    assert comp["abilities"]["blend"]["ability_list"][0]["name"] == "byte_edit"
    assert comp["abilities"]["gen_option"]["gen_count"] == 1


def test_parse_task_success_and_empty():
    node = {
        "task": {"status": 50},
        "total_image_count": 1, "finished_image_count": 1,
        "item_list": [{"image": {"large_images": [
            {"image_url": "https://x/1.png", "width": 2048, "height": 2048,
             "format": "png"}]}}],
    }
    st = protocol.parse_task("sid", node)
    assert st["ok"] is True and len(st["images"]) == 1

    empty = protocol.parse_task("sid", {"task": {"status": 50}})
    assert empty["ok"] is False and empty["failed"] is True  # 成功但无产物 = 失败


def test_parse_task_failure_message():
    st = protocol.parse_task("sid", {
        "task": {"status": 30}, "fail_starling_message": "生成失败",
    })
    assert st["failed"] is True and st["fail_message"] == "生成失败"


def test_partial_success_without_finish_is_not_terminal():
    """实跑抓到的瞬态：45 + finish_time=0 + 空 item_list 不是终态。"""
    st = protocol.parse_task("sid", {"task": {"status": 45, "finish_time": 0},
                                     "item_list": [], "total_image_count": 1,
                                     "finished_image_count": 0})
    assert st["finished"] is False and st["failed"] is False

    done = protocol.parse_task("sid", {
        "task": {"status": 45, "finish_time": 1795026116},
        "item_list": [{"image": {"large_images": [
            {"image_url": "https://x/1.png", "width": 2048, "height": 2048}]}}],
    })
    assert done["finished"] is True and done["ok"] is True
