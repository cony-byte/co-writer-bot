# -*- coding: utf-8 -*-
"""Natural-language callable tool registry.

The model can name only functions declared here.  Validation and risk policy are
code-owned; model confidence and model-supplied confirmation flags are ignored.
Adapters call the existing, battle-tested domain functions directly while the
large dispatch modules are migrated into smaller services over time.
"""
from __future__ import annotations

import base64
import re
import urllib.request
from dataclasses import dataclass
from typing import Callable


LOW = "low"
HIGH = "high"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict
    risk: str
    executor: Callable
    validator: Callable | None = None
    user_label: str | None = None

    def api_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def _object(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


WORK = {"type": "string", "description": "등록된 정식 작품명"}
EPISODE = {"type": "integer", "minimum": 1}
INSTRUCTION = {"type": "string", "description": "사용자 요청의 제약을 보존한 작업 지시"}
SCENE = {"type": "integer", "minimum": 1}
CUTS = {"type": "array", "items": {"type": "integer", "minimum": 1}, "maxItems": 50}
EPISODES = {"type": "array", "items": {"type": "integer", "minimum": 1}, "maxItems": 50}
ATTACHMENT_ID = {
    "type": "string",
    "description": "현재 Slack 메시지에 실제로 첨부된 이미지 파일 ID",
}


def _rest(args: dict, *, include_scene: bool = False, include_cuts: bool = False) -> str:
    parts = []
    if args.get("work"):
        parts.append(f"<{args['work']}>")
    if args.get("episode") is not None:
        parts.append(f"{args['episode']}화")
    if args.get("episodes"):
        parts.append(",".join(str(n) for n in args["episodes"]) + "화")
    if include_scene and args.get("scene") is not None:
        parts.append(f"씬{args['scene']}")
    if include_cuts and args.get("cuts"):
        parts.append("컷" + ",".join(str(n) for n in args["cuts"]))
    if args.get("instruction"):
        parts.append(args["instruction"])
    return " ".join(parts)


def _cw(name: str, args: dict, ctx) -> None:
    from . import dispatch_cowriter as cw
    channel, thread_ts = ctx.channel, ctx.thread_ts
    rest = _rest(args)
    if name == "generate_script":
        cw._do_generate(channel, thread_ts, rest)
    elif name == "revise_script":
        cw._do_revise(channel, thread_ts, args.get("instruction") or "")
    elif name == "edit_plan":
        cw._do_plan(channel, thread_ts, rest, in_thread=True)
    elif name == "review_script":
        cw._do_feedback(channel, thread_ts, rest, mode="both")
    elif name == "evaluate_fun":
        cw._do_feedback(channel, thread_ts, rest, mode="fun")
    elif name == "evaluate_logic":
        cw._do_feedback(channel, thread_ts, rest, mode="logic")
    elif name == "research_trends":
        cw._do_trend(channel, thread_ts, rest)
    elif name == "suggest_ideas":
        cw._do_idea(channel, thread_ts, rest)
    elif name == "sync_notion":
        cw._do_sync(channel, thread_ts, rest)
    elif name == "register_work_alias":
        cw._do_alias(channel, thread_ts, rest)
    elif name == "convert_script_format":
        cw._do_convert(channel, thread_ts, rest)


def _sb(name: str, args: dict, ctx) -> None:
    from . import dispatch_storyboard as sb
    channel, thread_ts, event = ctx.channel, ctx.thread_ts, ctx.event
    rest = _rest(args, include_scene=True, include_cuts=True)
    if name == "generate_scene_design":
        sb.sb_do_storyboard(channel, thread_ts, rest, stage=1)
    elif name == "generate_detail_conti":
        # ★2026-07-23 실측 — "<작품> N화 상세 콘티 작성해줘"가 이 스레드에 아직 씬 설계(1단계)가
        # 없으면 곧장 stage=2를 호출해 "아직 콘티가 없어요, [스토리보드] 3화처럼 해보세요"라는
        # 엉뚱한(화 번호도 예시일 뿐 실제 요청과 다름) 안내로 반려됐다 — 대본은 있는데도 그냥
        # 실패. sb_do_storyboard(stage=2) 대신, "1단계 없으면 1단계부터, 있으면 2단계로" 이미
        # 검증된 자동판단 로직(_do_storyboard_auto, [스토리보드] 명령이 쓰는 것과 동일)에
        # 위임한다 — 1단계가 없으면 먼저 씬 설계를 만들고, 사용자가 이어서 다시 요청하면
        # (이제 1단계가 있으니) 자연스럽게 2단계로 넘어간다.
        sb._do_storyboard_auto(channel, thread_ts, rest)
    elif name == "rewrite_conti":
        ok = sb._do_conti_rewrite(channel, thread_ts, args.get("instruction") or "", event,
                                  work=args.get("work"), episode=args.get("episode"),
                                  scene=args.get("scene"))
        if not ok:
            raise ValueError("수정할 콘티를 찾지 못했습니다")
    elif name == "generate_storyboard_grid":
        sb._do_images(channel, thread_ts, rest, feedback=args.get("instruction"))
    elif name == "generate_stillcuts":
        ref_data_url = (_attachment_data_url(args, ctx)
                        if args.get("attachment_id") else None)
        # ★2026-07-21: 노션 첨부 스토리보드를 구도 참조로 — 못 찾으면 조용히 자유생성으로
        # 폴백하지 않는다("임의로 생성하지 말고"는 명시적 부정 제약, 오전 nl_router 경로와
        # 동일한 원칙). 이미지를 찾을 때만 ref_data_url로 넘긴다.
        if not ref_data_url and args.get("use_notion_storyboard_ref"):
            ref_bytes = sb._notion_scene_reference_image(
                args.get("work"), args.get("episode"))
            if ref_bytes is None:
                from .shared.slack_io import _reply
                _reply(channel, thread_ts,
                       "노션 페이지에서 해당 화의 스토리보드 이미지를 못 찾았어요 — 노션에 "
                       "이미지가 잘 붙어있는지 확인해주시거나, 이 스레드에 이미지를 직접 "
                       "첨부해서 다시 요청해주세요. (요청하신 대로 구도를 임의로 생성하지는 "
                       "않았어요.)")
                return
            ref_data_url = "data:image/png;base64," + base64.b64encode(ref_bytes).decode("ascii")
        # ★2026-07-22: 이미지 여러 장을 첨부하면 업로드 순서대로 각 씬/컷의 구도 참조로 매핑한다
        # (이미지1→첫 대상, 이미지2→다음 대상…). 한 장이면 기존 단일 구도참조 그대로.
        ref_urls = _image_data_urls_in_order(ctx) if not args.get("use_notion_storyboard_ref") else []
        if len(ref_urls) > 1:
            sb._do_stills(channel, thread_ts, rest, feedback=args.get("instruction"),
                          ref_data_urls=ref_urls)
        else:
            sb._do_stills(channel, thread_ts, rest, feedback=args.get("instruction"),
                          ref_data_url=ref_data_url)
    elif name == "save_stillcuts":
        images = _image_attachments_in_order(ctx)
        if not images:
            raise ValueError("저장할 첨부 이미지를 찾지 못했습니다")
        ok = sb._do_save_stills_from_attachments(
            channel, thread_ts, images,
            work=args.get("work"), scene_num=args.get("scene"),
            cut_nums=args.get("cuts"), episode=args.get("episode"),
            instruction=args.get("instruction"),
            who=(event.get("user") if event else None))
        if not ok:
            raise ValueError("첨부 이미지를 저장하지 못했습니다")
    elif name == "save_videos":
        videos = _video_attachments_in_order(ctx)
        if not videos:
            raise ValueError("저장할 첨부 영상을 찾지 못했습니다")
        ok = sb._do_save_videos_from_attachments(
            channel, thread_ts, videos,
            work=args.get("work"), scene_num=args.get("scene"),
            cut_nums=args.get("cuts"), episode=args.get("episode"),
            instruction=args.get("instruction"),
            who=(event.get("user") if event else None))
        if not ok:
            raise ValueError("첨부 영상을 저장하지 못했습니다")
    elif name == "explain_stage_skip":
        sb._do_stage_skip_help(channel, thread_ts)
    elif name == "generate_video":
        # ★2026-07-21 실사용 사고: "씬5, 씬6 영상화"를 모델이 씬별 두 호출로 나눴는데 여기서
        # scene/cuts 인자를 버리고 instruction 원문만 넘겨서, 실행부가 텍스트에서 첫 씬(씬5)만
        # 두 번 파싱 — 씬6 호출까지 "씬5 확정 저장 안 됨" 경고가 중복으로 나갔다.
        # ★2026-07-22: 그리드→영상 경로 — 스틸이 없어 스틸부터 만들 때, 첨부한 그리드 이미지를
        # 스틸 생성의 구도 참조로 넘긴다(있으면 첫 이미지).
        _grid_refs = _image_data_urls_in_order(ctx)
        ok = sb._do_video_from_last_still(channel, thread_ts,
                                          args.get("instruction") or "영상으로 만들어줘",
                                          work=args.get("work"),
                                          scene=args.get("scene"),
                                          cut_nums=args.get("cuts"),
                                          ref_data_url=(_grid_refs[0] if _grid_refs else None))
        if not ok:
            raise ValueError("영상화할 스틸컷을 찾지 못했습니다")
    elif name == "compile_episode":
        sb._do_compile(channel, thread_ts, rest)
    elif name == "run_autopilot":
        sb._do_autopilot(channel, thread_ts, rest)
    elif name == "show_episode_status":
        sb._do_episode_status(channel, thread_ts, rest)
    elif name == "change_visual_style":
        # 전용 style 슬롯을 rest에 추가로 싣는다(instruction은 _rest가 이미 포함 — 중복 방지).
        sb._do_style(channel, thread_ts, f"{rest} {args.get('style') or ''}".strip())
    elif name == "finalize_conti":
        sb._do_conti_final(channel, thread_ts, rest, event)
    elif name == "save_conti_to_notion":
        sb._do_save_conti(channel, thread_ts, rest=rest)
    elif name == "reset_episode_outputs":
        sb._do_reset_episode(channel, thread_ts, rest)
    elif name == "export_file":
        sb.sb_do_export(channel, thread_ts, rest)
    elif name == "cancel_current_job":
        sb._CANCEL.add(thread_ts)
        sb.generator.cancel_prefix(thread_ts)
        sb.job_ledger.finish_by_thread(thread_ts)
        sb.interrupted_state.clear(thread_ts)
    elif name == "resume_interrupted_job":
        record = sb.interrupted_state.get(thread_ts)
        if not record:
            raise ValueError("재개할 작업이 없습니다")
        sb.interrupted_state.clear(thread_ts)
        # ★2026-07-23: kind!="plan"(콘티 재개)은 stage=2를 직접 호출했는데, 중단 이후 이
        # 스레드의 1단계 기록이 사라졌으면(예: 다른 화로 넘어감) generate_detail_conti와 같은
        # "화 번호도 다른 예시로 반려" 사고가 재현될 수 있다 — _do_storyboard_auto(1단계 없으면
        # 1단계부터, 있으면 2단계로)에 위임해 정상 케이스는 그대로 stage 2로 가고, 예외
        # 케이스만 안전하게 1단계부터 다시 잡게 한다.
        if record["kind"] == "plan":
            sb.sb_do_storyboard(channel, thread_ts, record["rest"], stage=1)
        else:
            sb._do_storyboard_auto(channel, thread_ts, record["rest"])


def _reference(name: str, args: dict, ctx) -> None:
    from . import dispatch_storyboard as sb
    kind = args["kind"]
    etype = sb._REF_TYPE_KW.get(kind.lower(), "person")
    display = args["name"]
    event = dict(ctx.event)
    if args.get("attachment_id"):
        event["files"] = [
            file for file in (ctx.event.get("files") or [])
            if str(file.get("id") or "") == str(args["attachment_id"])
        ]
    if name in ("register_reference_image", "replace_reference_image"):
        if not sb._do_typed_ref(ctx.channel, ctx.thread_ts, event,
                                work=args.get("work"), etype=etype, names=[display]):
            raise ValueError("첨부 이미지를 등록하지 못했습니다")
        return
    query = args.get("instruction") or f"{display} 이미지 생성해줘"
    if sb._do_element_ref_generate(ctx.channel, ctx.thread_ts, query, event,
                                   work=args.get("work"), name=display, etype=etype):
        return
    if not sb._do_element_gen(ctx.channel, ctx.thread_ts, event,
                              work=args.get("work"), name=display, etype=etype):
        raise ValueError("참조 이미지를 생성하지 못했습니다")


def _reference_many(name: str, args: dict, ctx) -> None:
    counts = {}
    for element in args["elements"]:
        key = (element.get("kind"), element.get("name"))
        counts[key] = counts.get(key, 0) + 1
    for element in args["elements"]:
        merged = {"work": args.get("work"), **element}
        key = (element.get("kind"), element.get("name"))
        if name == "generate_reference_image" and counts.get(key, 0) > 1:
            # Multiple age/look variants of one character must not overwrite the same
            # reference label. The user-authored instruction becomes the distinct label.
            merged["name"] = str(element.get("instruction") or "").strip()
        _reference(name, merged, ctx)


def _rename_reference(args: dict, ctx) -> None:
    from . import dispatch_storyboard as sb
    kind = args.get("kind")
    etype = sb._REF_TYPE_KW.get(str(kind).lower()) if kind else None
    sb._do_rename_ref(ctx.channel, ctx.thread_ts, work=args.get("work"), etype=etype,
                      old_name=args.get("old_name"), new_name=args.get("new_name"))


def _show_media(args: dict, ctx) -> None:
    from . import dispatch_storyboard as sb
    sb._do_show_media(ctx.channel, ctx.thread_ts, work=args.get("work"),
                      episode=args.get("episode"), scene=args.get("scene"),
                      cut=args.get("cut_number"), name=args.get("name"),
                      kind=args.get("kind"), all_scenes=args.get("all_scenes"))


def _delete_reference(args: dict, ctx) -> None:
    from . import dispatch_storyboard as sb
    kind = args.get("kind")
    etype = sb._REF_TYPE_KW.get(str(kind).lower()) if kind else None
    sb._do_delete_ref(ctx.channel, ctx.thread_ts, work=args.get("work"),
                      name=args.get("name"), etype=etype)


def _restore_reference(args: dict, ctx) -> None:
    from . import dispatch_storyboard as sb
    kind = args.get("kind")
    etype = sb._REF_TYPE_KW.get(str(kind).lower()) if kind else None
    sb._do_restore_ref(ctx.channel, ctx.thread_ts, work=args.get("work"),
                       name=args.get("name"), etype=etype)


def _still_variant(args: dict, ctx) -> None:
    from . import dispatch_storyboard as sb
    sb._do_still_variant(ctx.channel, ctx.thread_ts, work=args.get("work"),
                         scene=args.get("scene"), cut_number=args.get("cut_number"),
                         change=args.get("change"), episode=args.get("episode"))


def _echo_understanding(args: dict, ctx) -> None:
    """실제 작업 실행 전에 '이렇게 이해했어요'로 해석을 먼저 알린다(오해 조기 발견용)."""
    from .shared.slack_io import _reply
    summary = (args.get("summary") or "").strip()
    if summary:
        _reply(ctx.channel, ctx.thread_ts, f"📌 이렇게 이해했어요: {summary}")


def _set_work_style_note(args: dict, ctx) -> None:
    from . import dispatch_storyboard as sb
    sb._do_set_art_note(ctx.channel, ctx.thread_ts, work=args.get("work"), note=args.get("note"))


def _delete_media(args: dict, ctx) -> None:
    from . import dispatch_storyboard as sb
    sb._do_delete_media(ctx.channel, ctx.thread_ts, work=args.get("work"),
                        episode=args.get("episode"), scene=args.get("scene"),
                        cut=args.get("cut_number"), kind=args.get("kind"))


def _check_cut_seconds(args: dict, ctx) -> None:
    from . import dispatch_storyboard as sb
    sb._do_check_cut_seconds(ctx.channel, ctx.thread_ts, work=args.get("work"),
                             episode=args.get("episode"), scene=args.get("scene"))


def _replace_logo(args: dict, ctx) -> None:
    from . import dispatch_storyboard as sb
    selected = _selected_attachment(args, ctx)
    label = "방송 로고" if args["logo_type"] == "broadcast_logo" else "작품 로고"
    if args["scope"] == "future_default":
        if not sb._do_typed_ref(ctx.channel, ctx.thread_ts,
                                {"files": [selected]}, work=args.get("work"),
                                etype="prop", names=[label]):
            raise ValueError("로고 기본 참조를 등록하지 못했습니다")
        return
    ref_data_url = _attachment_data_url(args, ctx)
    rest = _rest(args, include_scene=True, include_cuts=False)
    rest = f"{rest} 컷{args['cut_number']}".strip()
    instruction = args.get("instruction") or f"컷{args['cut_number']}의 {label}를 첨부 이미지로 교체"
    sb._do_stills(ctx.channel, ctx.thread_ts, rest, feedback=instruction,
                  ref_data_url=ref_data_url)


def _validate_attachment(args: dict, ctx) -> str | None:
    attachment_id = args.get("attachment_id")
    files = ctx.event.get("files") or []
    by_id = {str(f.get("id") or ""): f for f in files}
    if not attachment_id:
        return "첨부 이미지가 필요해요."
    selected = by_id.get(str(attachment_id))
    if selected is None:
        return "선택한 첨부 이미지를 현재 메시지에서 찾지 못했어요. 이미지를 다시 첨부해 주세요."
    if not str(selected.get("mimetype") or "").startswith("image/"):
        return "참조 등록에는 이미지 첨부가 필요해요."
    return None


def _selected_attachment(args: dict, ctx) -> dict:
    attachment_id = str(args.get("attachment_id") or "")
    for file in (ctx.event.get("files") or []):
        if str(file.get("id") or "") == attachment_id:
            return file
    raise ValueError("선택한 첨부 이미지를 현재 메시지에서 찾지 못했어요.")


def _media_attachments_in_order(ctx, kind: str) -> list[tuple[bytes, str]]:
    """Download every attachment of a mimetype family (kind="image"|"video") on the
    *current* Slack event, in upload order.

    save_stillcuts / save_videos save N attachments straight to N cuts, so — unlike the
    single attachment_id tools — they must not rely on the model enumerating each file id
    (fragile for many files). This reads the real files[] off the validated event and
    maps them by position. Returns [(bytes, mimetype), ...]; skips other families and
    files without a private URL.
    """
    from . import config
    default_mime = "image/png" if kind == "image" else "video/mp4"
    out: list[tuple[bytes, str]] = []
    for file in (ctx.event.get("files") or []):
        if not str(file.get("mimetype") or "").startswith(kind + "/"):
            continue
        url = file.get("url_private_download") or file.get("url_private")
        if not url:
            continue
        request = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {config.SLACK_BOT_TOKEN}"}
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read()
        if data:
            out.append((data, str(file.get("mimetype") or default_mime)))
    return out


def _image_attachments_in_order(ctx) -> list[tuple[bytes, str]]:
    return _media_attachments_in_order(ctx, "image")


def _image_data_urls_in_order(ctx) -> list[str]:
    """현재 이벤트의 첨부 이미지 전부를 업로드 순서대로 data URL 리스트로(★2026-07-22 —
    다중 첨부를 씬/컷에 순서대로 구도 참조로 매핑하기 위함)."""
    return [f"data:{mt};base64,{base64.b64encode(b).decode('ascii')}"
            for b, mt in _image_attachments_in_order(ctx)]


def _video_attachments_in_order(ctx) -> list[tuple[bytes, str]]:
    return _media_attachments_in_order(ctx, "video")


def _validate_save_stillcuts(args: dict, ctx) -> str | None:
    images = [f for f in (ctx.event.get("files") or [])
              if str(f.get("mimetype") or "").startswith("image/")]
    if not images:
        return "저장할 이미지를 이 메시지에 첨부해 주세요."
    return None


def _validate_save_videos(args: dict, ctx) -> str | None:
    videos = [f for f in (ctx.event.get("files") or [])
              if str(f.get("mimetype") or "").startswith("video/")]
    if not videos:
        return "저장할 영상(mp4)을 이 메시지에 첨부해 주세요."
    return None


def _attachment_data_url(args: dict, ctx) -> str:
    """Download only the already-validated current-event Slack image."""
    from . import config
    selected = _selected_attachment(args, ctx)
    url = selected.get("url_private_download") or selected.get("url_private")
    if not url:
        raise ValueError("첨부 이미지 원본 URL을 찾지 못했습니다")
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {config.SLACK_BOT_TOKEN}"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read()
    if not data:
        raise ValueError("첨부 이미지 원본이 비어 있습니다")
    mimetype = str(selected.get("mimetype") or "image/png")
    return f"data:{mimetype};base64,{base64.b64encode(data).decode('ascii')}"


def _validate_optional_attachment(args: dict, ctx) -> str | None:
    return _validate_attachment(args, ctx) if args.get("attachment_id") else None


def _validate_reference_batch(args: dict, ctx, *, attachments_required: bool) -> str | None:
    seen = set()
    base_counts = {}
    for element in args.get("elements") or []:
        base = (element.get("kind"), element.get("name"))
        base_counts[base] = base_counts.get(base, 0) + 1
    for index, element in enumerate(args.get("elements") or []):
        base = (element.get("kind"), element.get("name"))
        instruction = str(element.get("instruction") or "").strip()
        key = base if attachments_required else (*base, instruction)
        if key in seen:
            return f"{index + 1}번째 대상이 앞선 대상과 겹쳐요: {key[1]}"
        seen.add(key)
        if not attachments_required and base_counts[base] > 1 and not instruction:
            return f"{index + 1}번째 {base[1]} 이미지에 구분할 설명이 필요해요."
        if attachments_required or element.get("attachment_id"):
            error = _validate_attachment(element, ctx)
            if error:
                return f"{index + 1}번째 이미지: {error}"
    return None


def _validate_replace_logo(args: dict, ctx) -> str | None:
    error = _validate_attachment(args, ctx)
    if error:
        return error
    if args.get("scope") == "current_cut" and not args.get("cut_number"):
        return "현재 컷의 로고를 바꾸려면 컷 번호가 필요해요."
    return None


def _validate_resume(_args: dict, ctx) -> str | None:
    context = getattr(ctx, "context", None) or {}
    return None if context.get("interrupted_job") else "재개할 중단 작업이 없어요."


def _register(spec: ToolSpec) -> ToolSpec:
    TOOLS[spec.name] = spec
    return spec


TOOLS: dict[str, ToolSpec] = {}

_USER_LABELS = {
    "generate_script": "창작 텍스트 만들기",
    "revise_script": "대본 수정하기",
    "edit_plan": "기획안 수정하기",
    "review_script": "대본 피드백하기",
    "evaluate_fun": "재미와 몰입도 살펴보기",
    "evaluate_logic": "개연성과 논리 살펴보기",
    "research_trends": "트렌드 조사하기",
    "suggest_ideas": "아이디어 제안하기",
    "sync_notion": "노션 자료 동기화하기",
    "register_work_alias": "작품 별칭 등록하기",
    "convert_script_format": "대본 형식 변환하기",
    "generate_scene_design": "씬 설계 만들기",
    "generate_detail_conti": "상세 콘티 만들기",
    "rewrite_conti": "상세 콘티 수정하기",
    "generate_storyboard_grid": "스토리보드 이미지 만들기",
    "generate_stillcuts": "스틸컷 만들기·다시 만들기",
    "save_stillcuts": "첨부 이미지를 스틸컷으로 저장하기",
    "save_videos": "첨부 영상을 저장하기",
    "explain_stage_skip": "첨부로 단계 건너뛰는 법 안내하기",
    "generate_video": "영상 만들기",
    "compile_episode": "회차 영상 합본 만들기",
    "run_autopilot": "제작 단계 이어서 진행하기",
    "show_episode_status": "제작 진행 상황 확인하기",
    "change_visual_style": "이미지 스타일 바꾸기",
    "finalize_conti": "콘티 최종본 확정하기",
    "save_conti_to_notion": "상세 콘티를 노션에 저장하기",
    "reset_episode_outputs": "회차 결과 초기화하기",
    "export_file": "결과 파일로 내보내기",
    "cancel_current_job": "진행 중인 작업 중단하기",
    "resume_interrupted_job": "중단된 작업 이어서 하기",
    "register_reference_image": "참고 이미지 등록하기",
    "replace_reference_image": "참고 이미지 바꾸기",
    "generate_reference_image": "참고 이미지 만들기",
    "register_reference_images": "여러 참고 이미지 등록하기",
    "generate_reference_images": "여러 참고 이미지 만들기",
    "rename_reference": "참고 이미지 이름 바꾸기",
    "replace_logo": "로고 적용하기",
}


def _add(name, desc, props, required, risk, executor, validator=None):
    _register(ToolSpec(name, desc, _object(props, required), risk, executor, validator,
                       _USER_LABELS.get(name)))


_writing = {
    "generate_script": ("작품의 로그라인, 개요, 줄거리, 대본, 인물 외모·비주얼 스펙, "
                        "룩앤필 또는 캐릭터 시트 같은 창작 텍스트를 생성한다."),
    "revise_script": "현재 스레드의 기존 대본이나 초안을 수정한다.",
    "edit_plan": "작품 기획안을 생성하거나 수정한다.",
    "review_script": "대본의 재미와 개연성을 함께 검토한다.",
    "evaluate_fun": "대본의 재미와 몰입도를 평가한다.",
    "evaluate_logic": "대본의 개연성과 논리를 평가한다.",
    "research_trends": "등록된 레퍼런스 데이터에서 트렌드를 조사한다.",
    "suggest_ideas": "작품 또는 일반 창작 아이디어를 제안한다.",
    "sync_notion": "사용자가 노션 URL을 주거나 '동기화'라고 명시했을 때만 그 자료를 동기화한다. 노션에 있는 자료를 참고해 다른 결과를 만들라는 요청에는 쓰지 않는다.",
    "register_work_alias": "사용자가 작품의 새 별칭·약칭을 추가해 달라고 명시했을 때만 등록한다. 방송명·프로그램명·로고 이름 고정에는 쓰지 않는다.",
    "convert_script_format": "대본 형식을 다른 포맷으로 변환한다.",
}
for _name, _desc in _writing.items():
    _risk = HIGH if _name in ("sync_notion", "register_work_alias") else LOW
    _add(_name, _desc, {"work": WORK, "episode": EPISODE, "episodes": EPISODES,
                        "instruction": INSTRUCTION},
         ["instruction"], _risk, lambda args, ctx, n=_name: _cw(n, args, ctx))

_storyboard = {
    "generate_scene_design": ("대본을 장면 단위 1단계 씬 설계로 변환한다. 상세 콘티나 이미지 생성이 아니다.", LOW),
    "generate_detail_conti": ("씬 설계나 대본을 컷 단위 상세 콘티 텍스트로 변환한다. scene을 생략하면 회차 전체다.", LOW),
    "rewrite_conti": ("기존 상세 콘티의 지정 범위를 수정한다. '수정/바꿔/손봐/다듬어'라는 원문 자체가 instruction이므로 추가 수정 방향을 묻지 않는다.", HIGH),
    "generate_storyboard_grid": ("상세 콘티를 여러 컷이 배치된 스토리보드 이미지 또는 그리드 이미지로 생성한다. 스틸컷 생성과 다르다.", HIGH),
    "generate_stillcuts": ("현재 스토리보드의 지정 씬·컷 또는 최근 스틸컷을 생성·재생성한다. 현재 메시지의 첨부 스토리보드를 그대로 참고하라는 요청이면 attachment_id를 반드시 포함한다. 활성 스레드에서는 작품·회차가 없어도 호출한다. ★이미지를 여러 장 첨부하고 여러 씬(예: 씬2,3,4)을 요청하면 씬을 나눠 여러 번 호출하지 말고 한 번의 호출에 씬을 다 담아라 — 첨부 이미지가 업로드 순서대로 각 씬의 구도 참조로 매핑된다.", HIGH),
    "save_stillcuts": ("사용자가 첨부한 이미지 자체를 재생성 없이 그대로 그 씬의 스틸컷으로 저장한다. '이 이미지들을 씬N 컷1,2,3으로 저장해줘', '내가 준 그림 그대로 스틸컷으로', '새로 만들지 말고 이대로 저장'처럼 첨부 이미지를 결과물로 굳히라는 요청. 여러 장이면 업로드 순서대로 컷에 매핑한다. 봇이 새로 그리는 generate_stillcuts와 정반대다. 활성 스레드에서는 작품·회차가 없어도 호출한다.", HIGH),
    "save_videos": ("사용자가 첨부한 완성 영상(mp4) 자체를 재생성 없이 그대로 그 씬 컷의 영상으로 저장한다. '이 영상들을 씬N 컷1,2,3 영상으로 저장해줘', '내가 만든 영상 그대로 넣어줘'처럼 첨부 영상을 결과물로 굳혀 영상 단계를 건너뛰고 바로 합본으로 가려는 요청. 여러 개면 업로드 순서대로 컷에 매핑한다. 활성 스레드에서는 작품·회차가 없어도 호출한다.", HIGH),
    "generate_video": ("현재 스토리보드나 최근 스틸컷의 지정 씬·컷을 영상으로 생성한다. 활성 스레드에서는 작품·회차가 없어도 호출한다.", HIGH),
    "compile_episode": ("생성된 영상들을 회차 합본으로 만든다.", HIGH),
    "run_autopilot": ("여러 제작 단계를 자동으로 연속 실행한다.", HIGH),
    "show_episode_status": ("작품 회차의 현재 제작 진행상황을 조회한다.", LOW),
    "change_visual_style": ("작품의 이후 이미지·영상 생성 스타일(화풍)을 변경한다. '겨울 하루 2D 애니메이션 스타일로 해줘', '이 작품 실사풍으로 바꿔줘'처럼. 바꿀 스타일명을 반드시 style 인자에 넣는다('실사풍' 또는 '2D 애니메이션').", HIGH),
    "finalize_conti": ("사용자가 콘티 자체를 최종본으로 저장·확정해 달라고 명시했을 때만 확정한다. 다른 작업의 전제로 '확정했어'라고 설명한 경우에는 쓰지 않는다.", HIGH),
    "save_conti_to_notion": ("현재 상세 콘티를 작품 노션에 저장한다.", HIGH),
    "reset_episode_outputs": ("지정 회차의 생성 결과를 초기화한다.", HIGH),
    "export_file": ("현재 결과를 파일로 내보낸다.", LOW),
}
for _name, (_desc, _risk) in _storyboard.items():
    _required = [] if _name not in ("export_file",) else ["instruction"]
    if _name in ("generate_scene_design", "generate_detail_conti", "compile_episode",
                 "run_autopilot", "show_episode_status", "reset_episode_outputs"):
        _required = ["episode"]
    if _name == "rewrite_conti":
        _required = ["instruction"]
    _props = {"work": WORK, "episode": EPISODE, "scene": SCENE, "cuts": CUTS,
              "instruction": INSTRUCTION}
    _validator = None
    if _name == "generate_stillcuts":
        _props["attachment_id"] = ATTACHMENT_ID
        # ★2026-07-21(회귀 코퍼스 notion-storyboard-composition-lock): "노션에 첨부해둔
        # 스토리보드 이미지 보고 구도 그대로" 요청 — Slack 첨부가 아니라 노션 페이지의
        # 해당 화 '스토리보드' 토글 안 이미지를 구도 참조로 쓴다. 이미지 회수는 코드
        # (_notion_scene_reference_image)가 하고 모델은 이 플래그만 세운다.
        _props["use_notion_storyboard_ref"] = {
            "type": "boolean",
            "description": "사용자가 노션에 이미 첨부해둔 스토리보드 이미지를 구도 참조로 쓰라고 한 경우 true",
        }
        _validator = _validate_optional_attachment
    if _name == "change_visual_style":
        # ★2026-07-21: 스타일명을 담을 명시 슬롯 — 없으면 '겨울 하루 2D 애니메이션 스타일로 해줘'
        # 에서 스타일 텍스트가 _do_style까지 안 가 파싱 실패했다(_rest는 instruction만 실음).
        _props["style"] = {"type": "string",
                           "description": "바꿀 스타일: '실사풍'(리얼리스틱) 또는 '2D 애니메이션'"}
    if _name == "save_stillcuts":
        _validator = _validate_save_stillcuts
    if _name == "save_videos":
        _validator = _validate_save_videos
    _add(_name, _desc,
         _props,
         _required,
         _risk, lambda args, ctx, n=_name: _sb(n, args, ctx), _validator)

_add("cancel_current_job", "현재 스레드에서 실행 중인 작업을 중단한다.", {}, [], HIGH,
     lambda args, ctx: _sb("cancel_current_job", args, ctx))
_add("resume_interrupted_job", "재개 버튼에서만 현재 스레드의 중단된 직전 작업을 재개한다. 자연어 긍정에는 호출하지 않는다.", {}, [], HIGH,
     lambda args, ctx: _sb("resume_interrupted_job", args, ctx), _validate_resume)
_add("explain_stage_skip",
     "첨부 파일로 제작 단계를 건너뛰는 방법을 안내한다. 사용자가 '첨부로 단계 건너뛰는 법'을 묻거나, 지원되지 않는 조합(예: 스토리보드 그리드를 콘티 없이 스틸컷으로 쓰려는 경우)을 시도해 명확한 실행 경로가 없을 때 호출한다.",
     {}, [], LOW, lambda args, ctx: _sb("explain_stage_skip", args, ctx))

_REF_NAME_DESC = (
    "참조 이름. ★뒷모습/뒤태/측면/(과거) 같은 각도·시점·버전 구분 단어나 '-A'/'-B' 같은 "
    "버전 접미사가 있으면 절대 빼지 말고 이름에 그대로 포함해서 넘긴다 — 이 등록/조회는 "
    "정확한 이름 일치로 동작해서, 이 단어가 빠지면 다른(엉뚱한) 참조를 등록/조회/삭제/복원"
    "하게 된다.")

_ref_props = {
    "work": WORK,
    "episode": EPISODE,
    # 로고는 일반 소품 참조로 오등록하지 않고 아래 replace_logo 전용 tool이 처리한다.
    "kind": {"type": "string", "enum": ["인물", "의상", "장소", "소품"]},
    "name": {"type": "string", "description": _REF_NAME_DESC},
    "attachment_id": ATTACHMENT_ID,
    "instruction": INSTRUCTION,
}
_add("register_reference_image", "사용자가 등록·확정이라고 말한 첨부 이미지 한 장을 인물, 의상, 장소 또는 소품 참조로 새로 등록한다.",
     _ref_props, ["kind", "name", "attachment_id"], HIGH,
     lambda args, ctx: _reference("register_reference_image", args, ctx), _validate_attachment)
_add("replace_reference_image", "사용자가 교체·바꾸기·수정이라고 말한 기존 참조 이미지 한 장을 현재 첨부 이미지로 교체한다. 단순 등록에는 쓰지 않는다.",
     _ref_props, ["kind", "name", "attachment_id"], HIGH,
     lambda args, ctx: _reference("replace_reference_image", args, ctx), _validate_attachment)
_add("generate_reference_image", "인물, 의상, 장소 또는 소품의 새 참조 이미지를 생성한다. 외형을 텍스트 지시로 바꾸는 재생성도 포함한다.",
     _ref_props, ["kind", "name"], HIGH,
     lambda args, ctx: _reference("generate_reference_image", args, ctx),
     _validate_optional_attachment)

_ref_item = _object({
    "kind": _ref_props["kind"], "name": _ref_props["name"],
    "attachment_id": _ref_props["attachment_id"], "instruction": INSTRUCTION,
}, ["kind", "name"])
_ref_items = {"type": "array", "items": _ref_item, "minItems": 1, "maxItems": 20}
_add("register_reference_images",
     "첨부 이미지 여러 장을 순서에 맞춰 여러 인물·의상·장소·소품 참조로 일괄 등록한다.",
     {"work": WORK, "elements": _ref_items}, ["elements"], HIGH,
     lambda args, ctx: _reference_many("register_reference_image", args, ctx),
     lambda args, ctx: _validate_reference_batch(args, ctx, attachments_required=True))
_add("generate_reference_images",
     "여러 인물·의상·장소·소품의 참조 이미지를 일괄 생성한다.",
     {"work": WORK, "elements": _ref_items}, ["elements"], HIGH,
     lambda args, ctx: _reference_many("generate_reference_image", args, ctx),
     lambda args, ctx: _validate_reference_batch(args, ctx, attachments_required=False))

_add("rename_reference",
     "이미 등록된 참조(인물·의상·장소·소품)의 이름을 새 이름으로 바꾼다. '유나경 출연자룩-B를 출연자룩-A로 바꿔줘', '이 인물 이름 개명해줘'처럼 이름 변경 요청. 새로 등록(register)하거나 이미지를 교체(replace)하는 것과 다르며 첨부 이미지가 필요 없다. kind는 알면 넣고 애매하면 생략한다.",
     {"work": WORK,
      "kind": {"type": "string", "enum": ["인물", "의상", "장소", "소품"]},
      "old_name": {"type": "string", "description": "현재 등록된 이름"},
      "new_name": {"type": "string", "description": "바꿀 새 이름"}},
     ["old_name", "new_name"], HIGH, _rename_reference)

_add("show_media",
     "이미 만들어진 산출물을 Slack에 보여준다(열람 의도 — 새로 만드는 게 절대 아님). 대상: 등록 참조(인물·의상·장소·소품), 생성된 스틸컷, 생성·확정 영상·합본, 등록 당시 원본 사진, 상세콘티/씬설계 텍스트. 예: '이영 PD룩 보여줘', '7씬 1컷 스틸컷 보여줘', '스토리보드 보여줘', '씬3 영상 보여줘', '1화 합본 보여줘', '이영 원본 사진', '1화 상세콘티 보여줘', '씬설계 보여줘'. '보여줘'라는 단어가 없어도 이미 있는 결과를 확인하려는 의도(뭐야/어떻게 나왔어/어디 있어/확인/다시 보여줘)면 이 툴이다. '다시 보여줘'는 표시(이 툴), '다시 만들어/뽑아줘'는 생성(generate_*)이니 혼동 금지. 신호: name=참조/원본 이름, scene/cut_number=스틸컷·영상 위치, kind=무엇인지 힌트(인물/의상/장소/소품/스틸컷/스토리보드/영상/합본/원본/콘티/씬설계).",
     {"work": WORK, "episode": EPISODE, "scene": SCENE,
      "cut_number": {"type": "integer", "minimum": 1, "description": "스틸컷·영상의 컷 번호"},
      "kind": {"type": "string",
               "description": "무엇을 보여줄지 힌트: 인물/의상/장소/소품/스틸컷/영상/합본 등"},
      "name": {"type": "string",
               "description": "참조를 볼 때 그 이름(예: 이영 PD룩). ★인물 참조를 '뒷모습/뒤태/옆모습/측면' "
                              "등 특정 각도로 보고 싶다는 표현이 있으면 그 단어를 절대 빼지 말고 이름 "
                              "뒤에 그대로 붙여서 넘긴다(예: '하루 뒷모습 보여줘' → name='하루 뒷모습', "
                              "kind에도 넣지 말고 name에만). 이 단어가 빠지면 등록된 그 각도 참조를 "
                              "못 찾고 기본(정면) 참조가 대신 보인다."},
      "all_scenes": {"type": "boolean",
                     "description": "'스틸컷 전부/전체/다 보여줘'처럼 그 화 전 씬을 다 보려면 true"}},
     [], LOW, _show_media)

_add("delete_reference",
     "이미 등록된 참조(인물·의상·장소·소품)를 삭제한다. '과 배경 참조 삭제해줘', '이 인물 지워줘'처럼 잘못 등록했거나 필요 없어진 참조 제거 요청. 되돌릴 수 없는 작업이라 확인 버튼을 띄운 뒤 삭제한다. kind는 알면 넣고 애매하면 생략한다.",
     {"work": WORK,
      "kind": {"type": "string", "enum": ["인물", "의상", "장소", "소품"]},
      "name": {"type": "string", "description": f"삭제할 {_REF_NAME_DESC}"}},
     ["name"], HIGH, _delete_reference)

_add("restore_reference",
     "참조를 등록 당시의 원본 이미지로 되돌린다. '원본으로 되돌려줘', '얼굴 원래대로'처럼 face_ref 중화나 재생성 이전 원본으로 복원하려는 요청. 원본 백업(_originals)이 있는 참조만 가능하다.",
     {"work": WORK,
      "kind": {"type": "string", "enum": ["인물", "의상", "장소", "소품"]},
      "name": {"type": "string", "description": f"되돌릴 {_REF_NAME_DESC}"}},
     ["name"], HIGH, _restore_reference)

_add("still_variant",
     "이미 만든 특정 컷의 스틸컷을 구도·의상은 그대로 두고 일부(주로 표정)만 바꿔 재생성한다. '컷4 그대로인데 표정만 웃게 바꿔', '이 컷 구도 유지하고 표정만 바꿔줘'처럼 한 컷의 부분 수정 요청. 씬·컷 번호와 바꿀 내용(change)이 필요하다. 새 스틸컷 전체 생성과 다르다.",
     {"work": WORK, "episode": EPISODE, "scene": SCENE,
      "cut_number": {"type": "integer", "minimum": 1, "description": "바꿀 컷 번호"},
      "change": {"type": "string", "description": "바꿀 내용(예: 표정을 미소로)"}},
     ["scene", "cut_number", "change"], HIGH, _still_variant)

_add("delete_media",
     "생성된 스틸컷·영상·합본을 종류별·범위별로 나눠 삭제한다. kind로 종류 지정(스틸컷/영상/합본), 범위는 컷·씬·화. 예: '7씬 3컷 스틸컷 삭제'(컷), '씬2 영상 지워줘'(씬), '1화 영상 다 지워'(화 전체 영상만), '1화 합본만 삭제'(합본), '1화 스틸컷 전부 삭제'. 스틸컷·영상·합본 전부 한꺼번에는 reset_episode_outputs, 참조 이미지 삭제는 delete_reference로 별개. 확인 버튼 후 _trash로 옮겨 복구 가능. 합본은 화 단위(씬/컷 없음). scene 생략 시 그 종류를 화 전체에서, cut_number 생략 시 그 씬 전체.",
     {"work": WORK, "episode": EPISODE, "scene": SCENE,
      "cut_number": {"type": "integer", "minimum": 1, "description": "특정 컷만 지울 때"},
      "kind": {"type": "string", "description": "지울 종류: 스틸컷/영상/합본 (여러 개면 각각 호출 또는 함께 명시)"}},
     [], HIGH, _delete_media)

_add("echo_understanding",
     "실제 작업(생성·수정·삭제·영상화 등)을 실행하기 직전에, 요청을 어떻게 이해했는지 한 줄로 먼저 사용자에게 알린다. 예: '저연프 1화 씬7 2컷을 영상으로 만들기'. 조회·보여주기·단순 대화에는 쓰지 않는다.",
     {"summary": {"type": "string", "description": "요청을 어떻게 이해했는지 한 줄 요약(무엇을·어느 작품/화/씬/컷에)"}},
     ["summary"], LOW, _echo_understanding)

_add("set_work_style_note",
     "작품에 항상 적용할 고정 스타일 지시(자유 문장)를 등록·변경·해제한다. '이 작품은 원본 레퍼런스랑 똑같이 만들어', '겨울 하루는 항상 따뜻한 톤으로', '이 작품 스타일 항상 이렇게 고정해줘'처럼 그 작품의 모든 이미지·영상 생성에 매번 반영할 지시. 화풍 프리셋(실사풍/2D 애니메이션) 변경(change_visual_style)과 달리 자유 문장이다. note에 그 지시를 넣고, 해제는 note에 '지워줘' 등을 넣는다.",
     {"work": WORK, "note": {"type": "string", "description": "항상 반영할 고정 지시(자유 문장). 해제하려면 '지워줘' 등."}},
     [], LOW, _set_work_style_note)

_add("check_cut_seconds",
     "콘티의 컷 초수 합계가 씬 헤더의 목표 초수와 맞는지 검증해 보고한다. '컷 초수 합 맞는지 확인해줘', '씬2 몇 초야'처럼 시간 예산 검증 요청. 씬별로 [N초] 비트를 합산해 목표와 비교(±1초 이내면 OK)한다. scene을 주면 그 씬만 검증한다.",
     {"work": WORK, "episode": EPISODE, "scene": SCENE},
     [], LOW, _check_cut_seconds)

_add("replace_logo",
     "첨부 로고를 특정 현재 컷에만 반영하거나 앞으로 쓸 방송·작품 로고 기본 참조로 등록한다. 두 범위를 모두 요청하면 scope별로 두 번 호출한다.",
     {"work": WORK, "episode": EPISODE, "scene": SCENE,
      "scope": {"type": "string", "enum": ["current_cut", "future_default"]},
      "cut_number": {"type": "integer", "minimum": 1},
      "logo_type": {"type": "string", "enum": ["broadcast_logo", "work_logo"]},
      "attachment_id": {"type": "string"}, "instruction": INSTRUCTION},
     ["scope", "logo_type", "attachment_id"], HIGH, _replace_logo,
     _validate_replace_logo)


def api_tools() -> list[dict]:
    return [spec.api_schema() for spec in TOOLS.values()]


def all_specs() -> list[ToolSpec]:
    """Every registered ToolSpec (additive helper for the subscription agent router)."""
    return list(TOOLS.values())


def get(name: str) -> ToolSpec | None:
    return TOOLS.get(name)


def sanitize_user_text(value: str) -> str:
    """Replace implementation vocabulary before model text reaches Slack."""
    text = str(value or "")
    for name, spec in sorted(TOOLS.items(), key=lambda item: len(item[0]), reverse=True):
        text = text.replace(name, spec.user_label or "요청한 작업")
    replacements = {
        "attachment_id": "첨부 이미지", "sb_stage": "현재 제작 단계",
        "pending_id": "확인 요청", "requires_confirmation": "실행 확인",
        "cut_number": "컷 번호", "logo_type": "로고 종류",
        "episodes": "회차 범위", "episode": "회차", "scene": "씬",
        "cuts": "컷", "instruction": "반영할 내용", "scope": "적용 범위",
        "elements": "대상 목록", "kind": "종류", "work": "작품",
        "JSON schema": "요청 형식", "json schema": "요청 형식",
        "schema": "요청 형식", "handler": "처리 과정",
        "tool_call": "실행 요청", "tool": "기능",
        "context": "대화 내용", "arguments": "요청 내용",
    }
    for internal, public in replacements.items():
        text = re.sub(
            rf"(?<![A-Za-z0-9_]){re.escape(internal)}(?![A-Za-z0-9_])",
            public, text, flags=re.I,
        )
    return text


def hydrate_arguments(spec: ToolSpec, args: dict, context: dict | None) -> dict:
    """Fill only trusted thread defaults; never invent a model argument."""
    hydrated = dict(args or {})
    context = context or {}
    defaults = context.get("resolved_defaults") or {}
    if "work" in spec.parameters.get("properties", {}) and not hydrated.get("work"):
        if defaults.get("work"):
            hydrated["work"] = defaults["work"]
    if "episode" in spec.parameters.get("properties", {}) and hydrated.get("episode") is None:
        if defaults.get("episode") is not None:
            hydrated["episode"] = defaults["episode"]
        else:
            match = re.search(r"(?<!\d)(\d{1,3})\s*[화회]", str(context.get("_user_query") or ""))
            if match:
                hydrated["episode"] = int(match.group(1))
    if spec.name == "replace_logo" and hydrated.get("scope") == "current_cut" \
            and hydrated.get("cut_number") is None:
        query = str(context.get("_user_query") or "")
        match = re.search(r"(?<!\d)(\d{1,3})\s*컷", query)
        if match:
            hydrated["cut_number"] = int(match.group(1))
        elif re.search(r"(?:맨\s*)?첫\s*컷", query):
            hydrated["cut_number"] = 1
    required = spec.parameters.get("required", [])
    if "attachment_id" in required and not hydrated.get("attachment_id"):
        images = [item for item in (context.get("attachments") or [])
                  if str(item.get("mimetype") or "").startswith("image/")]
        if len(images) == 1 and images[0].get("id"):
            hydrated["attachment_id"] = str(images[0]["id"])
    if spec.name == "generate_stillcuts" and not hydrated.get("attachment_id"):
        images = [item for item in (context.get("attachments") or [])
                  if str(item.get("mimetype") or "").startswith("image/")]
        query = str(context.get("_user_query") or "")
        if (len(images) == 1 and images[0].get("id")
                and re.search(
                    r"첨부|이\s*(?:이미지|사진|스토리보드)|스토리보드.{0,12}그대로|그대로.{0,12}스틸컷",
                    query, re.I)):
            hydrated["attachment_id"] = str(images[0]["id"])
    return hydrated


_ARG_LABELS = {
    "work": "작품명", "episode": "회차", "episodes": "회차 범위",
    "scene": "씬 번호", "cuts": "컷 번호", "instruction": "작업 내용",
    "attachment_id": "첨부 이미지", "scope": "적용 범위",
    "cut_number": "컷 번호", "logo_type": "로고 종류", "kind": "이미지 종류",
    "name": "대상 이름", "elements": "등록·생성할 대상",
    "old_name": "현재 이름", "new_name": "새 이름",
}


def _argument_label(path: str) -> str:
    tail = path.rsplit(".", 1)[-1].split("[", 1)[0]
    return _ARG_LABELS.get(tail, "요청 내용")


def validate_schema(schema: dict, value, path: str = "arguments") -> str | None:
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            return "요청 형식을 확인하지 못했어요. 내용을 다시 적어주세요."
        for key in schema.get("required", []):
            if key not in value or value[key] in (None, ""):
                return f"필요한 정보가 빠졌어요: {_ARG_LABELS.get(key, '요청 내용')}"
        if schema.get("additionalProperties") is False:
            unknown = set(value) - set(schema.get("properties", {}))
            if unknown:
                return "요청 내용을 안전하게 확인하지 못했어요. 내용을 다시 적어주세요."
        for key, child in schema.get("properties", {}).items():
            if key in value and value[key] is not None:
                error = validate_schema(child, value[key], f"{path}.{key}")
                if error:
                    return error
    elif expected == "string" and not isinstance(value, str):
        return f"{_argument_label(path)} 형식이 올바르지 않아요."
    elif expected == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
        return f"{_argument_label(path)}는 숫자로 알려주세요."
    elif expected == "array":
        if not isinstance(value, list):
            return f"{_argument_label(path)} 형식이 올바르지 않아요."
        if len(value) > schema.get("maxItems", len(value)):
            return f"{_argument_label(path)}가 너무 많아요. 범위를 나눠서 요청해 주세요."
        if len(value) < schema.get("minItems", 0):
            return f"{_argument_label(path)}을 하나 이상 알려주세요."
        for index, item in enumerate(value):
            error = validate_schema(schema.get("items", {}), item, f"{path}[{index}]")
            if error:
                return error
    if "enum" in schema and value not in schema["enum"]:
        return f"{_argument_label(path)}을 다시 확인해 주세요."
    if isinstance(value, int) and "minimum" in schema and value < schema["minimum"]:
        return f"{_argument_label(path)}은 1 이상으로 알려주세요."
    return None


def validate_call(spec: ToolSpec, args: dict, ctx) -> str | None:
    error = validate_schema(spec.parameters, args)
    if error:
        return error
    work = args.get("work")
    context = getattr(ctx, "context", None) or {}
    registry = context.get("registered_works") or {}
    if work and isinstance(registry, dict) and registry and spec.name != "sync_notion":
        canonical = work if work in registry else None
        if canonical is None:
            for registered, aliases in registry.items():
                if work in (aliases or []):
                    canonical = registered
                    break
        if canonical is None:
            return f"등록되지 않은 작품명이에요: {work}. 먼저 작품을 동기화해 주세요."
        args["work"] = canonical
    if spec.validator:
        return spec.validator(args, ctx)
    return None
