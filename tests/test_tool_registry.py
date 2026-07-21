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
    assert error and "지원하지 않는 인자" in error


def test_wrong_argument_type_is_rejected():
    spec = tool_registry.get("generate_detail_conti")
    error = tool_registry.validate_call(
        spec, {"work": "작품", "episode": "1"}, _ctx()
    )
    assert error and "정수" in error


def test_episode_workflows_require_episode():
    spec = tool_registry.get("generate_detail_conti")
    error = tool_registry.validate_call(spec, {"work": "작품"}, _ctx())
    assert error and "episode" in error


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
    assert error and "elements[1]" in error


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


if __name__ == "__main__":
    tests = [value for name, value in globals().copy().items() if name.startswith("test_")]
    for test in tests:
        test()
    print(f"{len(tests)} tool registry tests passed")
