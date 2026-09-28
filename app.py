# -*- coding: utf-8 -*-
import json
import logging
import time
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse, JSONResponse

logger = logging.getLogger("proxy")
logger.setLevel(logging.DEBUG)
formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
console = logging.StreamHandler(); console.setFormatter(formatter)
file_handler = logging.FileHandler("proxy.log", encoding="utf-8"); file_handler.setFormatter(formatter)
logger.addHandler(console); logger.addHandler(file_handler); logger.propagate = False

app = FastAPI()
UPSTREAM = "https://你自己的中转站/v1"
FORCE_MODEL = "deepseek-v4-pro"
MAX_TOKENS = 8000


def _extract_text_parts(content):
    parts = []
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for it in content:
            if isinstance(it, dict) and it.get("type") == "text":
                parts.append(it.get("text", ""))
    return parts


def _format_args(args):
    """把工具参数格式化成自然语言形式"""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            return args
    if isinstance(args, dict):
        return "，".join(f"{k}={v!r}" for k, v in args.items())
    return str(args)


def _build_history_block(assistant_msgs, tool_msgs, id_to_name):
    """
    把一组连续的 assistant / tool 消息合并成一条自然语言的背景说明。
    """
    lines = []
    for m in assistant_msgs:
        # assistant 的文本部分
        text_parts = _extract_text_parts(m.get("content"))
        for t in text_parts:
            if t and t.strip():
                lines.append(t.strip())
        # assistant 的 tool_calls
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            name = fn.get("name", "未知工具")
            args = _format_args(fn.get("arguments", ""))
            lines.append(f"（我之前用过 {name} 工具，条件是：{args}）")
    for m in tool_msgs:
        tcid = m.get("tool_call_id")
        name = id_to_name.get(tcid, "那个工具")
        c = m.get("content")
        if isinstance(c, str):
            txt = c
        elif isinstance(c, list):
            txt = "\n".join(it.get("text", "") for it in c
                            if isinstance(it, dict) and it.get("type") == "text")
        else:
            txt = str(c) if c else ""
        txt = txt.strip()
        # 结果太长时截断
        if len(txt) > 4000:
            txt = txt[:4000] + "\n...(已截断)"
        lines.append(f"（{name} 工具当时返回的内容是：\n{txt}）")
    return "\n".join(lines).strip()


def normalize_messages(messages, req_id=""):
    """
    把所有 assistant / tool 消息合并成自然语言背景，附加到最近一条 user 消息前面。
    最终只保留 system + user 消息，彻底避开 thinking 校验。
    """
    if not isinstance(messages, list):
        return messages

    # 建立 tool_call_id -> name 的映射
    id_to_name = {}
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                tcid = tc.get("id")
                name = (tc.get("function") or {}).get("name", "未知工具")
                if tcid:
                    id_to_name[tcid] = name

    new_msgs = []
    pending_assistants = []
    pending_tools = []

    def flush_pending():
        nonlocal pending_assistants, pending_tools
        if not pending_assistants and not pending_tools:
            return
        block = _build_history_block(pending_assistants, pending_tools, id_to_name)
        if block:
            new_msgs.append({"role": "user", "content": f"（以下是我之前的操作记录）\n{block}"})
            logger.debug(f"[{req_id}] 合并历史块: {len(block)} 字符")
        pending_assistants = []
        pending_tools = []

    for i, m in enumerate(messages):
        if not isinstance(m, dict):
            continue
        role = m.get("role")

        if role == "assistant":
            pending_assistants.append(m)
        elif role == "tool":
            pending_tools.append(m)
        elif role == "system":
            flush_pending()
            new_msgs.append(m)
        elif role == "user":
            flush_pending()
            c = m.get("content")
            if isinstance(c, list):
                m["content"] = [it for it in c
                                if not (isinstance(it, dict) and it.get("type") == "thinking")]
            new_msgs.append(m)
        else:
            # 未知 role，跳过
            logger.warning(f"[{req_id}] 未知 role: {role}")

    flush_pending()

    messages[:] = new_msgs
    return messages


def clean_content(content):
    if isinstance(content, list):
        return [it for it in content if it.get("type") == "text"]
    return content


def clean_response_json(data):
    if isinstance(data, dict) and "choices" in data:
        for choice in data["choices"]:
            msg = choice.get("message")
            if isinstance(msg, dict):
                msg.pop("reasoning_content", None)
                if "content" in msg:
                    msg["content"] = clean_content(msg["content"])
    return data


def clean_sse_line(line: bytes) -> bytes:
    if not line.startswith(b"data: "):
        return line
    payload = line[6:].strip()
    if payload == b"[DONE]" or not payload:
        return line
    try:
        obj = json.loads(payload)
    except Exception:
        return line
    if isinstance(obj, dict) and "choices" in obj:
        for choice in obj["choices"]:
            delta = choice.get("delta")
            if isinstance(delta, dict):
                delta.pop("reasoning_content", None)
                if "content" in delta:
                    delta["content"] = clean_content(delta["content"])
    return b"data: " + json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n"


def clean_sse_chunk(chunk: bytes) -> bytes:
    return b"".join(clean_sse_line(l) for l in chunk.splitlines(keepends=True))


def preview(data, limit=400):
    if isinstance(data, bytes):
        try:
            data = data.decode("utf-8", errors="replace")
        except Exception:
            return repr(data[:limit])
    s = str(data)
    return s[:limit] + ("..." if len(s) > limit else "")


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    req_id = str(int(time.time() * 1000))[-6:]
    logger.info(f"===== [{req_id}] 收到新请求 =====")

    raw_body = await request.body()
    try:
        body = json.loads(raw_body)
    except Exception as e:
        logger.error(f"[{req_id}] JSON 解析失败: {e}")
        return JSONResponse({"error": "invalid json"}, status_code=400)

    body["model"] = FORCE_MODEL

    if "messages" in body:
        orig_count = len(body["messages"])
        normalize_messages(body["messages"], req_id)
        logger.info(f"[{req_id}] 消息规范化: {orig_count} -> {len(body['messages'])}")
        for i, m in enumerate(body["messages"]):
            role = m.get("role")
            c = m.get("content")
            ln = len(c) if isinstance(c, str) else sum(
                len(it.get("text", "")) for it in (c or [])
                if isinstance(it, dict)
            ) if isinstance(c, list) else 0
            logger.debug(f"[{req_id}] msg[{i}] role={role} len={ln}")

    body["thinking"] = {"type": "disabled", "budget_tokens": 0}
    body["enable_thinking"] = False
    body["reasoning_effort"] = "none"

    if "max_tokens" not in body or body["max_tokens"] > MAX_TOKENS:
        body["max_tokens"] = MAX_TOKENS

    headers = {
        "Authorization": request.headers.get("authorization", ""),
        "Content-Type": "application/json",
    }

    stream = bool(body.get("stream", False))
    logger.info(f"[{req_id}] stream={stream}, messages={len(body.get('messages', []))}, "
                f"tools={len(body.get('tools', []))}")

    timeout = httpx.Timeout(connect=30.0, read=None, write=30.0, pool=30.0)
    url = f"{UPSTREAM}/chat/completions"

    if stream:
        async def stream_upstream():
            chunk_count = 0
            has_done = False
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    async with client.stream("POST", url, headers=headers, json=body) as r:
                        logger.info(f"[{req_id}] 上游状态: {r.status_code}")

                        if r.status_code != 200:
                            err = await r.aread()
                            logger.error(f"[{req_id}] 上游错误: {preview(err, 800)}")
                            yield f'data: {json.dumps({"error": err.decode("utf-8", "ignore")})}\n\n'.encode("utf-8")
                            yield b"data: [DONE]\n\n"
                            return

                        async for chunk in r.aiter_bytes():
                            if not chunk:
                                continue
                            chunk_count += 1
                            cleaned = clean_sse_chunk(chunk)
                            if b"[DONE]" in cleaned:
                                has_done = True
                            yield cleaned

                        if not has_done:
                            yield b"data: [DONE]\n\n"
                logger.info(f"[{req_id}] 流式完成 chunks={chunk_count}")

            except Exception as e:
                logger.info(f"[{req_id}] 流式中断: {type(e).__name__}: {e}")

        return StreamingResponse(
            stream_upstream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                     "X-Accel-Buffering": "no"},
        )

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(url, headers=headers, json=body)
        logger.info(f"[{req_id}] 上游状态: {r.status_code}")
        if r.status_code != 200:
            logger.error(f"[{req_id}] 上游错误: {preview(r.content, 800)}")
            return Response(content=r.content, status_code=r.status_code,
                            media_type=r.headers.get("content-type", "application/json"))
        try:
            data = json.loads(r.content)
            data = clean_response_json(data)
            cleaned = json.dumps(data, ensure_ascii=False).encode("utf-8")
        except Exception as e:
            logger.warning(f"[{req_id}] 清理失败: {e}")
            cleaned = r.content
        return Response(content=cleaned, status_code=200, media_type="application/json")
    except Exception as e:
        logger.error(f"[{req_id}] 异常: {e}", exc_info=True)
        return Response(content=str(e), status_code=500)


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def fallback(request: Request, path: str):
    logger.warning(f"未处理路径: /{path}")
    return Response("Not Found", status_code=404)