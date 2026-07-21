# -*- coding: utf-8 -*-
"""Offline safety-boundary tests for native tool calls."""
from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from unittest.mock import patch

from bot import tool_registry


def _ctx(files=None, works=None):
    return SimpleNamespace(
        event={"files": files or []},
        context={"registered_works": works or {}},
    )


def test_unknown_tool_is_not_registered():
    assert tool_registry.get("globals") is None
    assert tool_registry.get("delete_everything") is None


def test_extra_arguments_are_rejected():
    spec = tool_registry.get("generate_detail_conti")
    error = tool_registry.validate_call(
        spec,
        {"work": "작품", "episode": 1, "shell_command": "rm -rf"},
        _ctx(),
    )
    assert error and "안전하게 확인하지 못했어요" in error
    assert "shell_command" not in error


def test_wrong_argument_type_is_rejected():
    spec = tool_registry.get("generate_detail_conti")
    error = tool_registry.validate_call(
        spec, {"work": "작품", "episode": "1"}, _ctx()
    )
    assert error and "회차는 숫자로" in error


def test_episode_workflows_require_episode():
    spec = tool_registry.get("generate_detail_conti")
    error = tool_registry.validate_call(spec, {"work": "작품"}, _ctx())
    assert error and "회차" in error


def test_attachment_must_exist_on_current_event():
    spec = tool_registry.get("register_reference_image")
    args = {
        "work": "작품", "kind": "인물", "name": "연우",
        "attachment_id": "F_NOT_HERE",
    }
    error = tool_registry.validate_call(
        spec, args, _ctx([{"id": "F_REAL", "mimetype": "image/png"}])
    )
    assert error and "찾지 못했" in error


def test_optional_attachment_is_validated_when_present():
    spec = tool_registry.get("generate_reference_image")
    args = {
        "work": "작품", "kind": "인물", "name": "연우",
        "attachment_id": "F_FAKE",
    }
    error = tool_registry.validate_call(spec, args, _ctx())
    assert error and "찾지 못했" in error


def test_stillcut_attachment_is_declared_and_current_event_validated():
    spec = tool_registry.get("generate_stillcuts")
    assert "attachment_id" in spec.parameters["properties"]
    error = tool_registry.validate_call(
        spec, {"episode": 1, "attachment_id": "F_FAKE"},
        _ctx([{"id": "F_REAL", "mimetype": "image/png"}]),
    )
    assert error and "찾지 못했" in error


def test_destructive_and_expensive_tools_are_high_risk():
    for name in (
        "cancel_current_job", "reset_episode_outputs", "run_autopilot",
        "generate_stillcuts", "generate_video", "replace_reference_image",
    ):
        assert tool_registry.get(name).risk == tool_registry.HIGH


def test_work_alias_is_canonicalized_and_unknown_work_is_rejected():
    spec = tool_registry.get("generate_detail_conti")
    args = {"work": "별칭", "episode": 1}
    error = tool_registry.validate_call(
        spec, args, _ctx(works={"정식 작품": ["별칭"]})
    )
    assert error is None
    assert args["work"] == "정식 작품"

    unknown = {"work": "없는 작품", "episode": 1}
    error = tool_registry.validate_call(
        spec, unknown, _ctx(works={"정식 작품": ["별칭"]})
    )
    assert error and "등록되지 않은" in error


def test_trusted_context_hydrates_work_episode_and_single_attachment():
    spec = tool_registry.get("register_reference_image")
    context = {
        "resolved_defaults": {"work": "정식 작품", "episode": 2},
        "attachments": [{"id": "F1", "mimetype": "image/png"}],
    }
    args = tool_registry.hydrate_arguments(
        spec, {"kind": "인물", "name": "연우"}, context
    )
    assert args["work"] == "정식 작품"
    assert args["attachment_id"] == "F1"

    still_args = tool_registry.hydrate_arguments(
        tool_registry.get("generate_stillcuts"), {"episode": 1},
        {**context, "_user_query": "이 스토리보드 그대로 1화 스틸컷을 만들고 싶어"},
    )
    assert still_args["attachment_id"] == "F1"


def test_stillcut_executor_passes_downloaded_reference_data_url():
    captured = {}
    fake_sb = types.ModuleType("bot.dispatch_storyboard")

    def do_stills(channel, thread_ts, rest, feedback=None, ref_data_url=None):
        captured.update(channel=channel, thread_ts=thread_ts, rest=rest,
                        feedback=feedback, ref_data_url=ref_data_url)

    fake_sb._do_stills = do_stills
    ctx = SimpleNamespace(
        channel="C", thread_ts="T",
        event={"files": [{"id": "F1", "mimetype": "image/png",
                           "url_private": "https://files.slack.test/F1"}]},
    )
    with patch.dict(sys.modules, {"bot.dispatch_storyboard": fake_sb}), \
            patch.object(tool_registry, "_attachment_data_url",
                         return_value="data:image/png;base64,REF"):
        tool_registry._sb("generate_stillcuts", {
            "episode": 1, "attachment_id": "F1",
            "instruction": "첨부 스토리보드 그대로",
        }, ctx)
    assert captured["ref_data_url"] == "data:image/png;base64,REF"
    assert captured["feedback"] == "첨부 스토리보드 그대로"


def test_attachment_downloader_uses_selected_slack_file_and_bearer_token():
    from bot import config
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b"image-bytes"

    def urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["authorization"] = request.get_header("Authorization")
        captured["timeout"] = timeout
        return Response()

    ctx = _ctx([{
        "id": "F2", "mimetype": "image/png",
        "url_private_download": "https://files.slack.test/F2",
    }])
    with patch.object(config, "SLACK_BOT_TOKEN", "xoxb-test"), \
            patch.object(tool_registry.urllib.request, "urlopen", side_effect=urlopen):
        value = tool_registry._attachment_data_url({"attachment_id": "F2"}, ctx)
    assert captured == {
        "url": "https://files.slack.test/F2",
        "authorization": "Bearer xoxb-test",
        "timeout": 30,
    }
    assert value == "data:image/png;base64,aW1hZ2UtYnl0ZXM="


def test_batch_reference_validates_every_attachment_before_execution():
    spec = tool_registry.get("register_reference_images")
    args = {"elements": [
        {"kind": "인물", "name": "하루", "attachment_id": "F1"},
        {"kind": "인물", "name": "겨울", "attachment_id": "MISSING"},
    ]}
    error = tool_registry.validate_call(
        spec, args, _ctx([{"id": "F1", "mimetype": "image/png"}])
    )
    assert error and "2번째 이미지" in error


def test_generation_batch_allows_distinct_variants_without_overwrite():
    spec = tool_registry.get("generate_reference_images")
    args = {"elements": [
        {"kind": "인물", "name": "오미란", "instruction": "오미란 52세 얼굴"},
        {"kind": "인물", "name": "오미란", "instruction": "오미란 29세 얼굴(회귀)"},
    ]}
    assert tool_registry.validate_call(spec, args, _ctx()) is None


def test_current_cut_logo_requires_cut_number():
    spec = tool_registry.get("replace_logo")
    args = {"scope": "current_cut", "logo_type": "broadcast_logo",
            "attachment_id": "F1"}
    error = tool_registry.validate_call(
        spec, args, _ctx([{"id": "F1", "mimetype": "image/png"}])
    )
    assert error and "컷 번호" in error


def test_explicit_episode_and_first_cut_are_hydrated_from_user_text():
    script = tool_registry.hydrate_arguments(
        tool_registry.get("generate_script"), {"instruction": "대본"},
        {"_user_query": "4050 타겟 1화 대본 써줘", "resolved_defaults": {}},
    )
    assert script["episode"] == 1
    logo = tool_registry.hydrate_arguments(
        tool_registry.get("replace_logo"),
        {"scope": "current_cut", "logo_type": "broadcast_logo", "attachment_id": "F1"},
        {"_user_query": "맨 첫 컷 로고를 바꿔", "resolved_defaults": {}},
    )
    assert logo["cut_number"] == 1


def test_resume_requires_machine_readable_interrupted_state():
    spec = tool_registry.get("resume_interrupted_job")
    assert tool_registry.validate_call(spec, {}, _ctx()) == "재개할 중단 작업이 없어요."
    ctx = _ctx()
    ctx.context["interrupted_job"] = {"kind": "conti"}
    assert tool_registry.validate_call(spec, {}, ctx) is None


def test_every_registered_tool_has_user_facing_label():
    assert tool_registry.TOOLS
    assert all(spec.user_label for spec in tool_registry.TOOLS.values())


def test_internal_terms_are_rewritten_before_user_output():
    text = tool_registry.sanitize_user_text(
        "generate_stillcuts tool_call의 attachment_id와 schema를 확인하세요"
    )
    for internal in ("generate_stillcuts", "tool_call", "attachment_id", "schema"):
        assert internal not in text
    assert "스틸컷" in text and "첨부 이미지" in text


def test_save_stillcuts_requires_an_image_attachment():
    spec = tool_registry.get("save_stillcuts")
    assert spec is not None and spec.risk == tool_registry.HIGH
    # no files at all → guided error
    assert tool_registry.validate_call(spec, {"scene": 1, "cuts": [1, 2, 3]}, _ctx())
    # a non-image file present but no image → still an error
    error = tool_registry.validate_call(
        spec, {"scene": 1}, _ctx([{"id": "F1", "mimetype": "text/plain"}])
    )
    assert error and "첨부" in error
    # one real image → passes validation
    assert tool_registry.validate_call(
        spec, {"scene": 1, "cuts": [1]},
        _ctx([{"id": "F1", "mimetype": "image/png"}]),
    ) is None


def test_save_stillcuts_executor_downloads_all_images_in_upload_order():
    captured = {}
    fake_sb = types.ModuleType("bot.dispatch_storyboard")

    def do_save(channel, thread_ts, images, *, work=None, scene_num=None,
                cut_nums=None, episode=None, instruction=None, who=None):
        captured.update(channel=channel, thread_ts=thread_ts, images=images,
                        work=work, scene_num=scene_num, cut_nums=cut_nums,
                        episode=episode, instruction=instruction, who=who)
        return True

    fake_sb._do_save_stills_from_attachments = do_save
    ctx = SimpleNamespace(
        channel="C", thread_ts="T",
        event={"user": "U1", "files": [
            {"id": "A", "mimetype": "image/png", "url_private": "https://s.test/A"},
            {"id": "B", "mimetype": "text/plain", "url_private": "https://s.test/B"},
            {"id": "C", "mimetype": "image/jpeg", "url_private_download": "https://s.test/C"},
        ]},
    )
    downloaded_urls = []

    class Response:
        def __init__(self, data):
            self._data = data

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            return self._data

    def urlopen(request, timeout):
        downloaded_urls.append(request.full_url)
        return Response(b"png-" + request.full_url[-1:].encode())

    with patch.dict(sys.modules, {"bot.dispatch_storyboard": fake_sb}), \
            patch.object(tool_registry.urllib.request, "urlopen", side_effect=urlopen):
        tool_registry._sb("save_stillcuts", {
            "work": "겨울 하루", "episode": 1, "scene": 1, "cuts": [1, 2, 3],
        }, ctx)

    # only the two images are downloaded, in order; the text file is skipped
    assert downloaded_urls == ["https://s.test/A", "https://s.test/C"]
    assert [data for data, _mime in captured["images"]] == [b"png-A", b"png-C"]
    assert [mime for _data, mime in captured["images"]] == ["image/png", "image/jpeg"]
    assert captured["scene_num"] == 1
    assert captured["cut_nums"] == [1, 2, 3]
    assert captured["work"] == "겨울 하루"
    assert captured["episode"] == 1
    assert captured["who"] == "U1"


def test_save_videos_requires_a_video_attachment_and_skips_non_videos():
    spec = tool_registry.get("save_videos")
    assert spec is not None and spec.risk == tool_registry.HIGH
    assert tool_registry.validate_call(spec, {"scene": 1, "cuts": [1]}, _ctx())
    # an image present but no video → still an error
    error = tool_registry.validate_call(
        spec, {"scene": 1}, _ctx([{"id": "F1", "mimetype": "image/png"}])
    )
    assert error and "영상" in error
    # one real video → passes
    assert tool_registry.validate_call(
        spec, {"scene": 1, "cuts": [1]},
        _ctx([{"id": "F1", "mimetype": "video/mp4"}]),
    ) is None


def test_save_videos_executor_downloads_only_videos_in_upload_order():
    captured = {}
    fake_sb = types.ModuleType("bot.dispatch_storyboard")

    def do_save(channel, thread_ts, videos, *, work=None, scene_num=None,
                cut_nums=None, episode=None, instruction=None, who=None):
        captured.update(videos=videos, work=work, scene_num=scene_num,
                        cut_nums=cut_nums, episode=episode, who=who)
        return True

    fake_sb._do_save_videos_from_attachments = do_save
    ctx = SimpleNamespace(
        channel="C", thread_ts="T",
        event={"user": "U1", "files": [
            {"id": "A", "mimetype": "video/mp4", "url_private": "https://s.test/A"},
            {"id": "B", "mimetype": "image/png", "url_private": "https://s.test/B"},
            {"id": "C", "mimetype": "video/quicktime", "url_private_download": "https://s.test/C"},
        ]},
    )
    downloaded = []

    class Response:
        def __init__(self, data):
            self._data = data

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            return self._data

    def urlopen(request, timeout):
        downloaded.append(request.full_url)
        return Response(b"mp4-" + request.full_url[-1:].encode())

    with patch.dict(sys.modules, {"bot.dispatch_storyboard": fake_sb}), \
            patch.object(tool_registry.urllib.request, "urlopen", side_effect=urlopen):
        tool_registry._sb("save_videos", {
            "work": "겨울 하루", "episode": 1, "scene": 1, "cuts": [1, 2],
        }, ctx)

    # only the two videos are downloaded, in order; the image is skipped
    assert downloaded == ["https://s.test/A", "https://s.test/C"]
    assert [data for data, _mime in captured["videos"]] == [b"mp4-A", b"mp4-C"]
    assert captured["scene_num"] == 1 and captured["cut_nums"] == [1, 2]
    assert captured["who"] == "U1"


def test_explain_stage_skip_is_low_risk_and_paramless():
    spec = tool_registry.get("explain_stage_skip")
    assert spec is not None
    assert spec.risk == tool_registry.LOW
    assert spec.parameters["properties"] == {}
    assert tool_registry.validate_call(spec, {}, _ctx()) is None


if __name__ == "__main__":
    tests = [value for name, value in globals().copy().items() if name.startswith("test_")]
    for test in tests:
        test()
    print(f"{len(tests)} tool registry tests passed")
