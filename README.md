# 如何在 Trae、Cursor、Windsurf 等 AI IDE 中开启或关闭模型的思考模式：一个通用本地代理方案

最近我在 Trae 这类 AI IDE 里接入第三方大模型时，遇到一个比较烦的问题：有些模型默认开启 Thinking / 思考模式，模型会长时间分析，最后导致断连、超时，或者任务卡住。

这个问题不只可能出现在 Trae 里，像 Cursor、Windsurf、Cline、Roo Code 或其他支持自定义 OpenAI 兼容接口的 AI IDE / AI 编程工具，也可能遇到类似情况。

本质原因很简单：

很多 AI IDE 只给我们提供了这些基础配置：

* API 地址
* 模型 ID
* API Key
* 上下文长度
* 最大输出
* 工具调用轮次

但是它们往往没有提供一个地方，让我们自由传递额外请求参数，比如：

```json
{
  "thinking": {
    "type": "disabled"
  }
}
```

或者类似：

```json
{
  "reasoning_effort": "low"
}
```

所以我的解决方案是：**在本机启动一个轻量级本地代理，让 AI IDE 先请求本地代理，再由本地代理转发到真正的模型 API，并在转发时自动注入开启或关闭思考模式的参数。**

## 一、适用场景

这个方法适合以下情况：

1. 你的 AI IDE 支持自定义 OpenAI Chat Completions API；
2. 你的模型服务支持额外参数控制思考模式；
3. AI IDE 本身没有提供填写额外请求体的地方；
4. 你想控制模型是“快速回答”还是“深度思考”；
5. 你想减少长时间思考导致的断连、超时、卡死问题。

例如：

```text
Trae / Cursor / Windsurf / Cline / Roo Code
        ↓
本地代理
        ↓
模型官方 API
```

## 二、原理

原本的请求链路是：

```text
AI IDE → 模型官方 API
```

现在改成：

```text
AI IDE → 本地代理 → 模型官方 API
```

以 GLM-5.1 Coding Plan 为例：

```text
AI IDE
  ↓
http://127.0.0.1:18080/v1/chat/completions
  ↓
本地代理自动添加 thinking 参数
  ↓
https://open.bigmodel.cn/api/coding/paas/v4/chat/completions
  ↓
GLM-5.1
```

这样做的好处是：

1. 不需要修改 AI IDE 本体；
2. 不需要等官方加功能；
3. 可以自己控制思考模式；
4. 可以顺便限制最大输出，减少超时和断连；
5. 仍然保持 OpenAI Chat Completions 格式，兼容性比较好。

## 三、准备本地代理文件

在桌面新建一个文件夹，比如：

```text
ai-ide-thinking-proxy
```

然后在里面新建一个文件：

```text
app.py
```

把下面这段代码复制进去：

```python
import os
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse, JSONResponse

app = FastAPI()

# 上游模型服务地址
# 这里以 GLM Coding Plan 为例
UPSTREAM = os.getenv(
    "UPSTREAM",
    "https://open.bigmodel.cn/api/coding/paas/v4"
)

# 模型 ID
FORCE_MODEL = os.getenv("FORCE_MODEL", "glm-5.1")

# 思考模式：
# disabled = 关闭思考
# enabled = 开启思考
THINKING_TYPE = os.getenv("THINKING_TYPE", "disabled")

# 最大输出 token，建议先保守一点，避免超时
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "8000"))


@app.get("/v1/models")
async def models():
    return JSONResponse({
        "object": "list",
        "data": [
            {
                "id": FORCE_MODEL,
                "object": "model",
                "owned_by": "custom"
            }
        ]
    })


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()

    # 强制模型 ID，避免 AI IDE 传错
    body["model"] = FORCE_MODEL

    # 核心：注入思考模式参数
    # 这里以 GLM 的 thinking.type 为例
    body["thinking"] = {
        "type": THINKING_TYPE
    }

    # 限制最大输出，减少长时间卡住
    if "max_tokens" not in body or body["max_tokens"] > MAX_TOKENS:
        body["max_tokens"] = MAX_TOKENS

    headers = {
        "Authorization": request.headers.get("authorization", ""),
        "Content-Type": "application/json"
    }

    stream = bool(body.get("stream", False))
    timeout = httpx.Timeout(connect=30.0, read=None, write=30.0, pool=30.0)

    if stream:
        async def stream_upstream():
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream(
                    "POST",
                    f"{UPSTREAM}/chat/completions",
                    headers=headers,
                    json=body
                ) as r:
                    async for chunk in r.aiter_bytes():
                        yield chunk

        return StreamingResponse(
            stream_upstream(),
            media_type="text/event-stream"
        )

    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(
            f"{UPSTREAM}/chat/completions",
            headers=headers,
            json=body
        )

    return Response(
        content=r.content,
        status_code=r.status_code,
        media_type=r.headers.get("content-type", "application/json")
    )
```

## 四、安装依赖并启动

Windows PowerShell 进入这个文件夹：

```powershell
cd "$env:USERPROFILE\Desktop\ai-ide-thinking-proxy"
```

安装依赖：

```powershell
pip install fastapi uvicorn httpx
```

启动本地代理：

```powershell
python -m uvicorn app:app --host 127.0.0.1 --port 18080
```

如果看到：

```text
Uvicorn running on http://127.0.0.1:18080
```

说明成功了。

注意：这个窗口不要关。关了之后，AI IDE 就连不上本地代理了。

## 五、AI IDE 里怎么填

只要你的 AI IDE 支持 OpenAI Chat Completions 格式，大概都可以这样填。

以 Trae 为例：

| 配置项     | 填写                          |
| ------- | --------------------------- |
| API 格式  | OpenAI Chat Completions 格式  |
| 自定义请求地址 | `http://127.0.0.1:18080/v1` |
| 完整 URL  | 关闭                          |
| 模型 ID   | `glm-5.1`                   |
| API 密钥  | 填你的模型服务 API Key             |
| 多模态     | 按模型实际情况选择                   |
| 模型展示名称  | 自定义，比如 `GLM-5.1 Fast`       |
| 输出上下文   | 建议先填 8000                   |
| 工具调用轮次  | 建议先填 30                     |

重点是：AI IDE 里不要直接填模型官方 URL，而是填本地代理地址：

```text
http://127.0.0.1:18080/v1
```

因为现在是：

```text
AI IDE → 本地代理 → 模型官方 API
```

## 六、如何关闭思考模式

默认代码里这一行就是关闭思考：

```python
THINKING_TYPE = os.getenv("THINKING_TYPE", "disabled")
```

所以直接启动：

```powershell
python -m uvicorn app:app --host 127.0.0.1 --port 18080
```

就是关闭思考模式。

这个模式适合：

* 普通改代码；
* 小 bug 修复；
* 简单解释报错；
* 不想让模型长时间分析；
* 避免 AI IDE 里断连或超时。

## 七、如何开启思考模式

如果你想开启思考模式，可以在 PowerShell 里这样启动：

```powershell
$env:THINKING_TYPE="enabled"
python -m uvicorn app:app --host 127.0.0.1 --port 18080
```

这个模式适合：

* 复杂 bug；
* 架构分析；
* 多文件重构；
* 需要模型认真规划的任务。

如果后面想重新关闭，可以改回：

```powershell
$env:THINKING_TYPE="disabled"
python -m uvicorn app:app --host 127.0.0.1 --port 18080
```

## 八、端口被占用怎么办

如果启动时报这个错误：

```text
WinError 10048
通常每个套接字地址只允许使用一次
```

说明端口被占用了。

比如 `10808` 经常是 Clash、v2rayN、sing-box 等代理软件使用的端口。

解决方法：换一个端口，比如 `18080`。

启动：

```powershell
python -m uvicorn app:app --host 127.0.0.1 --port 18080
```

AI IDE 里就填：

```text
http://127.0.0.1:18080/v1
```

如果你换成 `19090`，那 AI IDE 里也要对应改成：

```text
http://127.0.0.1:19090/v1
```

总之，启动端口和 AI IDE 里填写的端口必须一致。

## 九、不同模型的参数可能不一样

这篇文章里的代码主要以 GLM 的参数为例：

```json
{
  "thinking": {
    "type": "disabled"
  }
}
```

但不同模型服务的思考参数可能不一样。

有些模型可能使用：

```json
{
  "reasoning_effort": "low"
}
```

或者：

```json
{
  "reasoning_effort": "high"
}
```

所以如果你用的不是 GLM，需要根据你所使用模型的 API 文档修改这一段：

```python
body["thinking"] = {
    "type": THINKING_TYPE
}
```

比如你想改成 `reasoning_effort`，可以改成类似：

```python
body["reasoning_effort"] = os.getenv("REASONING_EFFORT", "low")
```

本质都是一样的：**AI IDE 不给填的参数，我们通过本地代理在转发时补上。**

## 十、建议配置

如果你经常因为模型想太久而断连，建议先这样配：

| 项目     | 建议              |
| ------ | --------------- |
| 思考模式   | disabled        |
| 输出上下文  | 8000            |
| 工具调用轮次 | 30              |
| 输入上下文  | 64000 或 128000  |
| 任务方式   | 分步执行，不要一上来全仓库分析 |

提示词里也可以加一句：

```text
不要长时间思考。每轮只处理必要文件，先定位问题，再做最小修改。如果需要更多上下文，先问我，不要自行长时间全仓库分析。
```

这样可以明显降低模型卡死、超时、断连的概率。

## 十一、注意事项

1. 这个方法只是本地转发，不是修改 AI IDE 本体。
2. API Key 仍然是从 AI IDE 传到本地代理，再转发给模型官方接口。
3. 建议只监听 `127.0.0.1`，不要改成 `0.0.0.0`，避免局域网其他设备访问。
4. 不同模型的思考参数不一定一样，需要根据模型文档调整。
5. 如果你用的不是 GLM-5.1，需要自己改 `FORCE_MODEL` 和上游 API 地址。
6. 如果你想更安全，可以只在自己电脑本地使用，不要把这个代理暴露到公网。

## 十二、总结

这个方法的核心就是一句话：

**AI IDE 没有提供思考模式开关，那就用本地代理在请求转发时自动补上参数。**

以 GLM 为例，关闭思考：

```json
{
  "thinking": {
    "type": "disabled"
  }
}
```

开启思考：

```json
{
  "thinking": {
    "type": "enabled"
  }
}
```

其实这个功能很简单，官方完全可以在自定义模型界面里加一个“额外请求体 / 自定义参数 / 思考模式开关”。

在官方支持之前，这个本地代理方案算是一个比较简单、可控、可复用的临时解决方案。不只适用于 Trae，也适用于其他支持 OpenAI 兼容自定义 API 的 AI IDE。
