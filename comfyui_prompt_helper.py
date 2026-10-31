# -*- coding: utf-8 -*-
"""
ComfyUI 提示词助手  (Python + tkinter + OpenAI SDK)
====================================================

功能
----
1. 点图片框选一张图（或按 Ctrl+V 粘贴截图），调用视觉大模型生成 **英文提示词**；
2. 同时给出 **中文翻译**，方便你看懂每条提示词在说什么；
3. 正负向提示词按「左列英文 / 右列中文」对照展示，额外出提示词解读，
   以及一个「英文提示词 → 中文」的翻译小工具；
4. 可选描述对象（整张图片 / 人物 / 背景），决定模型把笔墨集中在哪里；
5. 每次生成都存进本地 SQLite（history.db），「历史记录」页可以回看当时的图片和提示词。

运行
----
    python comfyui_prompt_helper.py

依赖（全局环境已装好）：tkinter / pillow / openai

配置
----
API_KEY / BASE_URL / MODEL 从本文件旁边的 .env 读取，改 .env 即可，不用动代码：

    API_KEY=sk-xxxxxxxxxxxx
    BASE_URL=https://api.deepseek.com
    MODEL=deepseek-flash

.env 缺失时退回「配置区」里的内置默认值。
"""

from __future__ import annotations

import base64
import io
import json
import os
import queue
import re
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import openai
from openai import OpenAI
from PIL import Image, ImageGrab, ImageOps, ImageTk

import history_store

# ==================================================================
#  配置区：参数从项目目录下的 .env 读取
# ==================================================================
# .env 就放在本文件旁边，内容形如：
#     API_KEY=sk-xxxxxxxxxxxxxxxxxxxx
#     BASE_URL=https://api.deepseek.com
#     MODEL=deepseek-flash
#
# 换了机器 / 换了 key 只改 .env，不用动代码；改完重启程序生效。
# .env 已经在 .gitignore 里，不会被提交到 git。
ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

# .env 缺失、或里面没写某一项时，退回这里的默认值（留空则程序会提示你去配）
DEFAULT_API_KEY = ""
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"

ENV_ALIASES = {
    "api_key": ("API_KEY", "OPENAI_API_KEY", "api_key", "openai_api_key"),
    "base_url": ("BASE_URL", "OPENAI_BASE_URL", "OPENAI_API_BASE", "base_url"),
    "model": ("MODEL", "OPENAI_MODEL", "model"),
}


def read_env_file(path: str) -> dict:
    """读 .env。优先用 python-dotenv；没装就用手写的简易解析（够用且无依赖）。"""
    if not os.path.exists(path):
        return {}
    try:
        from dotenv import dotenv_values
        values = dotenv_values(path)
        return {k.strip(): (v or "").strip() for k, v in values.items() if k}
    except Exception:
        pass
    data = {}
    with open(path, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
            data[key] = value.strip().strip('"').strip("'")
    return data


def pick_env(env: dict, names, default: str = "") -> str:
    for name in names:
        value = env.get(name)
        if value:
            return value
    return default


_ENV = read_env_file(ENV_FILE)

API_KEY = pick_env(_ENV, ENV_ALIASES["api_key"], DEFAULT_API_KEY)
BASE_URL = pick_env(_ENV, ENV_ALIASES["base_url"], DEFAULT_BASE_URL)
MODEL = pick_env(_ENV, ENV_ALIASES["model"], DEFAULT_MODEL)
CONFIG_SOURCE = ".env" if _ENV else "内置默认值"


def masked_key() -> str:
    """界面上显示 Key 时只露出首尾几位。"""
    key = (API_KEY or "").strip()
    if not key:
        return "（未配置）"
    if len(key) <= 10:
        return key[:2] + "***"
    return f"{key[:6]}…{key[-4:]}"


# 传输与图片参数（一般不用动）
TIMEOUT = 120.0           # 单次请求超时（秒）
MAX_RETRIES = 1           # SDK 自动重试次数
MAX_UPLOAD_SIDE = 1280    # 上传前把长边压到最多这么多像素，省 token
JPEG_QUALITY = 90

# ==================================================================
#  提示词模板
# ==================================================================
BASE_SYSTEM = """你是一位资深的 ComfyUI / Stable Diffusion 提示词工程师，也是最懂构图、用光与画风的分析师。

用户会给你一张图片，请你：
1. 用英文写出可以直接用于 ComfyUI「正向提示词」输入框的提示词；
2. 用英文写出配套的负向提示词；
3. 把两条提示词逐条翻译成中文，让中文用户能看懂每个词的意思；
4. 再用中文写 2~4 句「提示词解读」，说明你抓住了画面的哪些关键点（主体、光线、镜头、画风、质感）。

输出要求（非常重要）：
- 只输出一个 JSON 对象，不要 markdown 代码块、不要 ```json 标记、不要任何 JSON 以外的解释文字。
- JSON 必须严格使用下面 5 个键，值都是字符串：
{
  "positive_en": "英文正向提示词",
  "negative_en": "英文负向提示词",
  "positive_zh": "正向提示词的中文翻译",
  "negative_zh": "负向提示词的中文翻译",
  "tips_zh": "中文提示词解读"
}

内容规范：
- 英文提示词要具体、可执行，覆盖：主体与数量、动作姿态、服饰与材质、环境背景、光线（方向/软硬/色温）、构图与视角（景别/机位）、镜头与画质、艺术风格。
- 中文翻译要忠实、逐词对应，用中文常见的说法；专有名词可以写成「中文（English）」，例如「电影级布光（cinematic lighting）」。
- 负向提示词要包含 ComfyUI 常用的基础负面项，再补上这张图容易出现的瑕疵。
- 如果图中出现了可读的文字，可以在 tips_zh 里说明，但不要把整段文字抄进提示词。"""

STYLE_HINTS = {
    "标签式 · SD1.5 / SDXL（推荐）":
        "提示词风格：逗号分隔的英文标签（Danbooru 风格也接受），顺序按 "
        "画质词 → 主体 → 动作/姿态 → 服装 → 环境 → 光线 → 镜头 → 风格 → 画质细节；"
        "可以用权重语法（如 (masterpiece:1.2)）强调 1~2 处最关键的词。",
    "自然语言 · SD3 / Flux":
        "提示词风格：用 1~3 句连贯的英文自然语言描述画面，像给摄影师或画师的 brief，"
        "包含主体、环境、光线、镜头与质感；不要堆砌标签，不要用权重语法。",
    "动漫 / 二次元":
        "提示词风格：面向 anime 系模型（Pony / Illustrious / Animagine 等），使用二次元标签体系，"
        "写清人物数量、发型发色、瞳色、服装、表情、构图与画风（如 anime style, cel shading, vibrant colors）。",
    "写实摄影":
        "提示词风格：面向写实摄影模型，写清机位（close-up / full body / eye-level）、镜头焦段、景深、"
        "光源类型与方向、时间天气、肤色与材质细节、真实质感（如 RAW photo, film grain, 35mm）。",
    "图生图 · 保留原图构图":
        "提示词风格：用户将把图片用于 img2img / ControlNet 工作流。只描述画面内容、材质与风格，"
        "不要描述会改变原始布局的构图或视角信息，方便在重绘时保留原构图。",
    "技术插画 / 产品图":
        "提示词风格：面向科技感与技术说明图，写清材质、结构、光照布置、背景、棚拍或渲染质感"
        "（如 studio lighting, product photography, octane render, clean background）。",
}

# 描述对象：决定模型把笔墨集中在哪里
SUBJECT_WHOLE = "整张图片"
SUBJECT_HINTS = {
    SUBJECT_WHOLE:
        "【描述对象】整张图片：均衡覆盖画面里的主体、环境、光线、构图与风格，"
        "不要偏重某一部分，整幅画面都要能复现。",
    "人物":
        "【描述对象】画面中的人物：重点写人物本身——人数、性别与年龄感、发型发色、瞳色、表情、"
        "服装款式与材质、姿态动作、手持物与配饰、人物身上的光线与打光方式、镜头景别；"
        "环境只在和人物关系密切时一句话带过，不要大段铺陈背景。若画面里没有人物，"
        "就近选择画面中最像主体的对象来描述，并在 tips_zh 里说明画面中没有人物。",
    "背景":
        "【描述对象】环境与背景：重点写场景——地点、空间结构、建筑或自然元素、天气与时间、"
        "光源与光影、景深、氛围与色调、画面层次与远近关系；若画面里有人物，只用一句话交代其"
        "位置与作用，主体必须是环境，不要让人物描写抢戏。",
}

TRANSLATE_SYSTEM = """你是 ComfyUI / Stable Diffusion 提示词翻译专家。
用户会给你一段英文提示词（可能是逗号分隔的标签，也可能是自然语言）。
请输出：
1. 逐条对应的中文翻译，格式为「英文 —— 中文」，每条一行；
2. 最后用「小结：」开头，用两三句中文概括这段提示词描述的画面。
不要输出多余的解释，不要用 markdown 表格。"""

# ==================================================================
#  OpenAI 客户端
# ==================================================================
_client: "OpenAI | None" = None


def get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=API_KEY,
            base_url=(BASE_URL or None),
            timeout=TIMEOUT,
            max_retries=MAX_RETRIES,
            # 关掉压缩协商。openai 3.x 用的 httpx2 在解 brotli 时要求新版 brotli
            # （Decompressor.process(data, output_buffer_limit=...)），而本机 brotli 1.0.9
            # 的 process() 不认这个关键字参数，服务器一旦回 Content-Encoding: br 就会抛
            # "TypeError: process() takes no keyword arguments"。发 identity 让服务器不压缩即可绕过。
            default_headers={"Accept-Encoding": "identity"},
        )
    return _client


def config_ready() -> bool:
    key = (API_KEY or "").strip()
    return bool(key) and "填入" not in key and not key.startswith("sk-在此")


def config_help_text() -> str:
    """没配置好时的说明，直接告诉用户 .env 在哪、该写什么。"""
    return (f"请在项目目录下新建 / 编辑 .env 文件（和本程序放在一起）：\n{ENV_FILE}\n\n"
            "里面写三行：\n"
            "    API_KEY=你的Key\n"
            "    BASE_URL=https://api.deepseek.com\n"
            "    MODEL=deepseek-flash\n\n"
            f"当前读到：API_KEY = {masked_key()}\n"
            f"          BASE_URL = {BASE_URL or '（空）'}\n"
            f"          MODEL = {MODEL or '（空）'}\n"
            f"配置来源：{CONFIG_SOURCE}")


def humanize_api_error(e: BaseException) -> str:
    """把 SDK 异常翻译成一句能看懂的中文提示。"""
    if isinstance(e, openai.AuthenticationError):
        return ("鉴权失败（401）：API_KEY 不对或已失效。\n"
                f"请检查 .env 里的 API_KEY（当前：{masked_key()}）。\n\n"
                f"原始信息：{e}")
    if isinstance(e, openai.NotFoundError):
        return ("接口或模型不存在（404）：BASE_URL 路径或 MODEL 名字写错了。\n"
                "注意 BASE_URL 一般以 /v1、/v3、/v4 结尾，不要自己再加 /chat/completions。\n\n"
                f"原始信息：{e}")
    if isinstance(e, openai.RateLimitError):
        return ("被限流或额度不足（429）：稍等一会儿再试，或去控制台看看余额 / 并发限制。\n\n"
                f"原始信息：{e}")
    if isinstance(e, openai.BadRequestError):
        return ("请求被拒绝（400）：最常见的原因是「这个模型不支持图片输入」，"
                "其次是温度 / max_tokens 等参数不被支持。\n换一个视觉模型试试（如 glm-4.5v、qwen-vl-max）。\n\n"
                f"原始信息：{e}")
    if isinstance(e, openai.APITimeoutError):
        return (f"请求超时（{TIMEOUT:.0f} 秒）：网络慢或模型排队。可以减小图片、"
                "或把 TIMEOUT 调大再试。\n\n"
                f"原始信息：{e}")
    if isinstance(e, openai.APIConnectionError):
        return ("连不上服务：检查网络 / 代理，以及 BASE_URL 是否写对。\n\n"
                f"原始信息：{e}")
    if isinstance(e, openai.APIStatusError):
        return f"服务端返回错误（HTTP {e.status_code}）：{e}"
    msg = str(e)
    if isinstance(e, TypeError) and "process() takes no keyword arguments" in msg:
        return ("本地依赖的压缩解码问题，不是你的 Key 错了：\n"
                "openai 3.x 依赖的 httpx2 需要新版 brotli，本机 brotli 1.0.9 的 "
                "Decompressor.process() 不接受 output_buffer_limit 参数，"
                "服务器返回 br 压缩内容时就崩。\n\n"
                "应对：程序已经默认发送 Accept-Encoding: identity 来规避；"
                "如果还遇到，执行 pip install -U brotli 升级后重启程序。\n\n"
                f"原始信息：{type(e).__name__}: {msg}")
    return f"{type(e).__name__}: {e}"


# ==================================================================
#  业务逻辑
# ==================================================================
def load_image(path: str) -> Image.Image:
    im = Image.open(path)
    im = ImageOps.exif_transpose(im)
    return im


def prepare_upload_bytes(im: Image.Image) -> bytes:
    """把 PIL 图片压成 JPEG 字节串（长边 ≤ MAX_UPLOAD_SIDE）。

    返回的字节同时用于两处：base64 发给模型、原样存进历史记录的 image_blob，
    所以历史里看到的图就是当时实际发出去的那张。
    """
    w, h = im.size
    scale = min(1.0, MAX_UPLOAD_SIDE / float(max(w, h)))
    if scale < 1.0:
        im = im.resize(
            (max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)

    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        im = Image.alpha_composite(bg, rgba)
    im = im.convert("RGB")

    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


def build_messages(image_b64: str, style: str, extra: str,
                   subject: str = SUBJECT_WHOLE) -> list:
    system = (BASE_SYSTEM
              + "\n\n" + STYLE_HINTS.get(style, "")
              + "\n\n" + SUBJECT_HINTS.get(subject, SUBJECT_HINTS[SUBJECT_WHOLE]))
    user_text = "请分析这张图片，按约定的 JSON 格式输出提示词。"
    if extra.strip():
        user_text += f"\n用户补充要求（请一并考虑）：{extra.strip()}"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": [
            {"type": "text", "text": user_text},
            {"type": "image_url",
             "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
        ]},
    ]


def ask_vision(messages: list) -> tuple:
    """调用视觉模型，返回 (文本, 统计信息 dict)。

    response_format={"type": "json_object"} 只是「建议」，不是所有 OpenAI 兼容端点都认
    （有的直接回 400 unsupported parameter）。所以这里带一个降级：先带 response_format 请求，
    一旦被 400 拒绝，就去掉它原样重试一次；两条路都失败才把异常抛出去。
    是否降级记在 info["json_mode"] 里，界面「原始返回」页会照实写出来。
    """
    client = get_client()
    params = {"model": MODEL, "messages": messages, "temperature": 0.6}
    t0 = time.time()
    json_mode = True
    try:
        resp = client.chat.completions.create(
            **params, response_format={"type": "json_object"})
    except openai.BadRequestError:
        # 400 一般在模型推理之前就被拒了，不会额外烧 token，只多一次往返；
        # 但万一真的是「模型不支持图片」，第二次也会照样失败，错误照常冒出去。
        json_mode = False
        resp = client.chat.completions.create(**params)
    elapsed = time.time() - t0

    msg = resp.choices[0].message
    content = msg.content
    if isinstance(content, list):  # 少数服务返回富文本数组
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    info = {
        "elapsed": elapsed,
        "model": getattr(resp, "model", MODEL),
        "usage": getattr(resp, "usage", None),
        "json_mode": json_mode,
    }
    return (content or "").strip(), info


def ask_translate(text: str) -> tuple:
    client = get_client()
    t0 = time.time()
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": TRANSLATE_SYSTEM},
            {"role": "user", "content": text},
        ],
        temperature=0.3,
        max_tokens=1600,
    )
    elapsed = time.time() - t0
    content = resp.choices[0].message.content or ""
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return content.strip(), {"elapsed": elapsed, "model": getattr(resp, "model", MODEL)}


def extract_json(text: str) -> "dict | None":
    """从模型输出里尽最大努力抠出 JSON。"""
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t).strip()
    try:
        obj = json.loads(t)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    start, end = t.find("{"), t.rfind("}")
    if 0 <= start < end:
        try:
            obj = json.loads(t[start:end + 1])
            if isinstance(obj, dict):
                return obj
        except Exception:
            return None
    return None


KEY_ALIASES = {
    "positive_en": ("positive_en", "positive", "positive_prompt", "prompt_en",
                    "prompt", "en_prompt", "正向提示词", "english_prompt"),
    "negative_en": ("negative_en", "negative", "negative_prompt", "neg_prompt",
                    "负向提示词", "negative prompt"),
    "positive_zh": ("positive_zh", "positive_cn", "positive_zh_cn", "正向提示词中文",
                    "正向翻译", "positive_chinese"),
    "negative_zh": ("negative_zh", "negative_cn", "negative_zh_cn", "负向提示词中文",
                    "负向翻译", "negative_chinese"),
    "tips_zh": ("tips_zh", "tips", "note_zh", "explain_zh", "解读", "说明", "tips_cn"),
}


def pick(d: dict, key: str) -> str:
    for alias in KEY_ALIASES[key]:
        for k, v in d.items():
            if str(k).strip().lower() == alias.lower() and isinstance(v, str) and v.strip():
                return v.strip()
    return ""


def parse_result(text: str) -> dict:
    """把模型输出整理成固定字段；JSON 解析失败时退化为「原文当正向提示词」。"""
    out = {"positive_en": "", "negative_en": "", "positive_zh": "",
           "negative_zh": "", "tips_zh": "", "json_ok": False}
    obj = extract_json(text)
    if obj:
        got = {k: pick(obj, k) for k in out if k != "json_ok"}
        if got["positive_en"] or got["positive_zh"]:
            out.update(got)
            out["json_ok"] = True
            return out

    # 退化处理：按常见小标题（【Positive prompt】/**Negative Prompt:**/正向提示词：…）切分
    head = re.compile(
        r"\n[^\S\n]*(?:[#>*\-]+[^\S\n]*)?(?:【|\[|\*\*)?[^\S\n]*"
        r"(positive[^\S\n]*(?:prompt)?|negative[^\S\n]*(?:prompt)?|"
        r"正向提示词|负向提示词|正向|负向|中文翻译|翻译|提示词解读|解读)"
        r"[^\S\n]*(?:】|\]|\*\*)?[^\S\n]*[:：]?[^\S\n]*",
        re.I)
    sections = head.split("\n" + text)
    if len(sections) > 2:
        for i in range(1, len(sections) - 1, 2):
            name = sections[i].strip().lower()
            body = re.sub(r"^[*\s]+|[*\s]+$", "", sections[i + 1])
            if not body:
                continue
            if "negativ" in name or "负向" in name:
                out["negative_en"] = body
            elif "positiv" in name or "正向" in name:
                out["positive_en"] = body
            elif "中文" in name or "翻译" in name:
                out["positive_zh"] = body
            else:
                out["tips_zh"] = body
        if out["positive_en"]:
            return out

    out["positive_en"] = text.strip()
    out["tips_zh"] = "（模型没有按 JSON 格式返回，这里只做了兜底展示；可以重试一次。）"
    return out


def usage_text(usage) -> str:
    if usage is None:
        return "—"
    try:
        return f"prompt={usage.prompt_tokens}, completion={usage.completion_tokens}, total={usage.total_tokens}"
    except Exception:
        return str(usage)


def usage_total(usage) -> int:
    """只要 total_tokens，取不到就当 0（历史记录里存这个数）。"""
    try:
        return int(usage.total_tokens)
    except Exception:
        return 0


def format_record_text(rec: dict) -> str:
    """把一条历史记录排版成给人看的纯文本（详情页显示、复制整条都用它）。"""
    w, h = rec.get("image_w") or 0, rec.get("image_h") or 0
    size = f"原图 {w} × {h} px" if (w and h) else "尺寸未记录"
    lines = [
        f"时间：{rec.get('created_at') or '—'}",
        f"图片：{rec.get('image_name') or '（未知文件）'}（{size}；库里存的是上传用的压缩 JPEG）",
        f"模型：{rec.get('model') or '—'}    耗时：{float(rec.get('elapsed') or 0):.2f} s"
        f"    tokens：{rec.get('tokens_total') or '—'}",
        f"描述对象：{rec.get('subject') or '—'}    风格：{rec.get('style') or '—'}",
    ]
    extra = (rec.get("extra") or "").strip()
    if extra:
        lines.append(f"补充要求：{extra}")
    lines.append(f"JSON 解析：{'成功' if rec.get('json_ok') else '失败（已兜底）'}")
    for title, key in (("【正向提示词 · English】", "positive_en"),
                       ("【正向提示词 · 中文】", "positive_zh"),
                       ("【负向提示词 · English】", "negative_en"),
                       ("【负向提示词 · 中文】", "negative_zh"),
                       ("【提示词解读】", "tips_zh")):
        body = (rec.get(key) or "").strip()
        lines += ["", title, body or "（空）"]
    return "\n".join(lines)


# ==================================================================
#  界面
# ==================================================================
ACCENT = "#2563eb"
ACCENT_DARK = "#1d4ed8"
HEADER_BG = "#101a2e"
PANEL_BG = "#f6f8fc"
MUTED = "#64748b"
FONT_UI = ("Microsoft YaHei UI", 10)
FONT_SMALL = ("Microsoft YaHei UI", 9)
FONT_BOLD = ("Microsoft YaHei UI", 10, "bold")
FONT_MONO = ("Consolas", 10)


class PromptApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.scale = self._detect_scale()
        self.q: "queue.Queue[dict]" = queue.Queue()
        self.image_path = None
        self.image_size = None
        self._photo = None
        # 从「历史记录」载入的图：原图文件可能早就不在了，所以直接留着库里的 JPEG 字节
        self._history_image_blob = None
        self._history_image_name = ""
        self.topmost = False       # 窗口是否置顶（表头那个按钮切换）
        self.busy = False
        self._t0 = 0.0
        self._last = {}            # 最近一次生成结果
        self._raw = ""
        self._hist_photo = None    # 历史详情里的图片必须留引用，否则会被回收成空白
        self._hist_rec = None      # 当前选中的那条历史记录
        self.hist_error = ""
        self._last_image_dir = ""  # 上次选图的文件夹，选图对话框从这里开始
        try:
            self.db_file = history_store.init_db()
            self._last_image_dir = history_store.get_setting(
                "last_image_dir", "")
        except Exception as e:     # 数据库坏了不该让程序起不来，只在历史页里报
            self.db_file = history_store.DB_FILE
            self.hist_error = f"{type(e).__name__}: {e}"

        self._build_ui()
        self.root.after(120, self._drain)
        self.root.bind("<Control-o>", lambda e: self.choose_image())
        self.root.bind("<Control-v>", self._on_ctrl_v)
        self.root.bind("<Control-Return>", lambda e: self.generate())
        self.history_refresh()

    # ---------------- 界面搭建 ----------------
    def _detect_scale(self) -> float:
        """系统 DPI 缩放（125% -> 1.25），保证高分屏下窗口比例和字号一致。"""
        try:
            return max(1.0, self.root.winfo_fpixels("1i") / 96.0)
        except Exception:
            return 1.0

    def px(self, n):
        return int(round(n * self.scale))

    def _build_ui(self):
        self.root.title("ComfyUI 提示词助手 · 图片 → 英文提示词 + 中文翻译")
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w = min(self.px(1140), sw - self.px(80))
        h = min(self.px(840), sh - self.px(90))
        self.root.geometry(f"{w}x{h}+{self.px(40)}+{self.px(30)}")
        self.root.minsize(min(self.px(1000), w), min(self.px(720), h))
        self.root.configure(bg="#e9edf4")

        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("TNotebook.Tab", font=FONT_UI, padding=(14, 6))
        style.configure("TLabelframe.Label", font=FONT_BOLD)
        style.configure("TLabel", background=PANEL_BG, font=FONT_UI)
        style.configure("Hint.TLabel", foreground=MUTED, font=FONT_SMALL)
        style.configure("TFrame", background=PANEL_BG)
        style.configure("TLabelframe", background=PANEL_BG)

        # ---- 顶部标题栏 ----
        header = tk.Frame(self.root, bg=HEADER_BG)
        header.pack(fill="x")
        left = tk.Frame(header, bg=HEADER_BG)
        left.pack(side="left", padx=18, pady=10)
        tk.Label(left, text="ComfyUI 提示词助手", bg=HEADER_BG, fg="#ffffff",
                 font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w")
        tk.Label(left, text="图片 → 视觉大模型生成英文提示词 → 中文翻译，方便边学边用",
                 bg=HEADER_BG, fg="#93a6c6", font=FONT_SMALL).pack(anchor="w")
        # 置顶开关放最右边，方便一边看生成结果一边操作别的窗口
        self.btn_topmost = tk.Button(
            header, text="置顶窗口", command=self.toggle_topmost,
            bg="#1e2b45", fg="#cbd5e1", activebackground="#26344f",
            activeforeground="#ffffff", relief="flat", font=FONT_SMALL,
            padx=12, pady=4, cursor="hand2", bd=0)
        self.btn_topmost.pack(side="right", padx=(0, 10), pady=10)
        tk.Label(header, text=f"{MODEL}  @  {BASE_URL}   ·   {CONFIG_SOURCE}",
                 bg=HEADER_BG, fg="#7f93b5",
                 font=("Consolas", 9)).pack(side="right", padx=18)

        # ---- 主区域 ----
        main = ttk.Frame(self.root, padding=(12, 10))
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=0, minsize=self.px(372))
        main.columnconfigure(1, weight=1)
        main.rowconfigure(0, weight=1)

        left_col = tk.Frame(main, bg=PANEL_BG)
        left_col.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        right_col = tk.Frame(main, bg=PANEL_BG)
        right_col.grid(row=0, column=1, sticky="nsew")

        self._build_left(left_col)
        self._build_right(right_col)

        # ---- 底部状态栏 ----
        bar = tk.Frame(self.root, bg="#dde3ee")
        bar.pack(fill="x", side="bottom")
        self.status = tk.Label(bar, text="就绪", anchor="w", bg="#dde3ee", fg="#334155",
                               font=FONT_SMALL)
        self.status.pack(side="left", padx=12, pady=4)
        tk.Label(bar, text="Ctrl+O 选图   Ctrl+V 粘贴图片   Ctrl+Enter 生成",
                 bg="#dde3ee", fg="#7b8794", font=FONT_SMALL).pack(side="right", padx=12)

    def _build_left(self, parent):
        box = ttk.LabelFrame(parent, text=" ① 选择图片 ", padding=10)
        box.pack(fill="both", expand=True)

        holder = tk.Frame(box, bg="#eef2f8", height=self.px(344),
                          highlightthickness=2, highlightbackground="#dde3ee",
                          cursor="hand2")
        holder.pack(fill="x", pady=(2, 8))
        holder.pack_propagate(False)
        self.holder = holder

        self.preview = tk.Label(
            holder, text="点击这里选择图片\n\n或按 Ctrl+O\n也可以复制截图后按 Ctrl+V",
            bg="#eef2f8", fg="#9aa8bd", font=FONT_UI, justify="center", cursor="hand2")
        self.preview.pack(fill="both", expand=True)

        for w in (holder, self.preview):
            w.bind("<Button-1>", lambda e: self.choose_image())
            w.bind("<Enter>", lambda e: self._hover(True))
            w.bind("<Leave>", lambda e: self._hover(False))
            w.bind("<Button-3>", self._context_menu)

        self.info = tk.Label(box, text="尚未选择图片", bg=PANEL_BG, fg=MUTED,
                             font=FONT_SMALL, anchor="w", justify="left", wraplength=self.px(330))
        self.info.pack(fill="x")

        hint = ttk.Label(
            box,
            text=("小提示：点图片框可以换图；中文翻译只是帮你理解英文标签的意思，"
                  "真正粘贴进 ComfyUI 的应当是英文提示词。"),
            style="Hint.TLabel", wraplength=self.px(330), justify="left")
        hint.pack(fill="x", pady=(10, 0))

        steps = tk.Frame(box, bg="#eef2f8", highlightthickness=1,
                         highlightbackground="#d5dde9")
        steps.pack(fill="x", side="bottom", pady=(10, 0))
        tk.Label(steps, text="使用步骤", bg="#eef2f8", fg="#334155",
                 font=FONT_BOLD).pack(anchor="w", padx=10, pady=(8, 2))
        tk.Label(steps, bg="#eef2f8", fg="#5b6b82", font=FONT_SMALL, justify="left",
                 text=("1. 点图片框选一张想还原的图（Ctrl+V 粘贴截图）\n"
                       "2. 选描述对象和风格，点「生成提示词」\n"
                       "3. 复制左边的英文到 ComfyUI 正向提示词框\n"
                       "4. 右边的中文对照着看，理解每个词在说什么")).pack(
            anchor="w", padx=10, pady=(0, 10))

    def _hover(self, on):
        self.holder.configure(highlightbackground=ACCENT if on else "#dde3ee")

    def _context_menu(self, event):
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="选择图片…", command=self.choose_image)
        menu.add_command(label="粘贴剪贴板图片", command=self.paste_image)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def toggle_topmost(self):
        """切换「窗口置顶」：开着的时候窗口始终浮在别的程序上面，方便对照着抄提示词。"""
        want = not self.topmost
        try:
            self.root.attributes("-topmost", want)
        except tk.TclError as e:      # 极少见：某些窗口管理器不支持
            self.set_status(f"这个系统不支持窗口置顶：{e}")
            return
        self.topmost = want
        self.btn_topmost.configure(
            text="已置顶" if want else "置顶窗口",
            bg=ACCENT if want else "#1e2b45",
            fg="#ffffff" if want else "#cbd5e1",
            activebackground=ACCENT_DARK if want else "#26344f")
        self.set_status("窗口已置顶，再点一次取消" if want else "已取消置顶")

    def _build_right(self, parent):
        cfg = ttk.LabelFrame(parent, text=" ② 生成设置 ", padding=10)
        cfg.pack(fill="x")
        cfg.columnconfigure(3, weight=1)

        ttk.Label(cfg, text="描述对象").grid(
            row=0, column=0, sticky="w", padx=(0, 8))
        self.subject_var = tk.StringVar(value=SUBJECT_WHOLE)
        self.subject_combo = ttk.Combobox(
            cfg, textvariable=self.subject_var, values=list(SUBJECT_HINTS),
            state="readonly", font=FONT_UI, width=9)
        self.subject_combo.grid(
            row=0, column=1, sticky="w", pady=2, padx=(0, 18))

        ttk.Label(cfg, text="提示词风格").grid(
            row=0, column=2, sticky="w", padx=(0, 8))
        self.style_var = tk.StringVar(value=list(STYLE_HINTS)[0])
        combo = ttk.Combobox(cfg, textvariable=self.style_var, values=list(STYLE_HINTS),
                             state="readonly", font=FONT_UI)
        combo.grid(row=0, column=3, sticky="ew", pady=2)
        self.combo = combo

        ttk.Label(cfg, text="补充要求").grid(row=1, column=0,
                                         sticky="w", padx=(0, 8), pady=(6, 0))
        self.extra_var = tk.StringVar()
        entry = ttk.Entry(cfg, textvariable=self.extra_var, font=FONT_UI)
        entry.grid(row=1, column=1, columnspan=3, sticky="ew", pady=(6, 0))
        entry.bind("<Return>", lambda e: self.generate())
        self.entry = entry

        actions = tk.Frame(cfg, bg=PANEL_BG)
        actions.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(10, 0))

        self.btn_generate = tk.Button(
            actions, text="生成提示词", command=self.generate, bg=ACCENT, fg="white",
            activebackground=ACCENT_DARK, activeforeground="white", relief="flat",
            font=("Microsoft YaHei UI", 11, "bold"), padx=22, pady=7, cursor="hand2", bd=0)
        self.btn_generate.pack(side="left")

        self.btn_save = tk.Button(
            actions, text="保存结果", command=self.save_result, bg="#e2e8f0", fg="#334155",
            relief="flat", font=FONT_UI, padx=14, pady=7, cursor="hand2", bd=0)
        self.btn_save.pack(side="left", padx=8)

        self.btn_clear = tk.Button(
            actions, text="清空结果", command=self.clear_result, bg="#e2e8f0", fg="#334155",
            relief="flat", font=FONT_UI, padx=14, pady=7, cursor="hand2", bd=0)
        self.btn_clear.pack(side="left")

        self.progress = ttk.Progressbar(
            actions, mode="determinate", value=0, length=140)
        self.progress.pack(side="right", padx=(10, 0))

        # ---- 结果区 ----
        nb = ttk.Notebook(parent)
        nb.pack(fill="both", expand=True, pady=(10, 0))
        nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self.nb = nb

        # Tab 1 提示词：左列英文、右列中文；上行正向、下行负向
        tab_prompt = tk.Frame(nb, bg=PANEL_BG, padx=8, pady=8)
        nb.add(tab_prompt, text="提示词")
        for c in (0, 1):
            tab_prompt.columnconfigure(c, weight=1, uniform="prompt_col")
        tab_prompt.rowconfigure(0, weight=3, uniform="prompt_row")
        tab_prompt.rowconfigure(1, weight=2, uniform="prompt_row")

        self.text_pos_en, self.btn_copy_pos_en = self._grid_block(
            tab_prompt, 0, 0, "English · 正向提示词（复制进 ComfyUI 正向框）", FONT_MONO)
        self.text_pos_zh, self.btn_copy_pos_zh = self._grid_block(
            tab_prompt, 0, 1, "中文 · 正向翻译（看懂就行，别粘进 ComfyUI）", FONT_UI)
        self.text_neg_en, self.btn_copy_neg_en = self._grid_block(
            tab_prompt, 1, 0, "English · 负向提示词（整段填进负向框）", FONT_MONO)
        self.text_neg_zh, self.btn_copy_neg_zh = self._grid_block(
            tab_prompt, 1, 1, "中文 · 负向翻译", FONT_UI)
        self.idx_prompt = nb.index(tab_prompt)

        # Tab 2 提示词解读
        tab_tips = tk.Frame(nb, bg=PANEL_BG, padx=10, pady=8)
        nb.add(tab_tips, text="提示词解读")
        self.text_tips, self.btn_copy_tips = self._text_block(
            tab_tips, "中文 · 模型为什么这样写（学习用）", 12, FONT_UI, 1)

        # Tab 3 翻译工具
        tab_tr = tk.Frame(nb, bg=PANEL_BG, padx=10, pady=8)
        nb.add(tab_tr, text="翻译工具")
        ttk.Label(tab_tr, text="粘贴一段英文提示词（或自己写的英文描述），翻译成中文并逐条解释：",
                  style="Hint.TLabel").pack(anchor="w", pady=(0, 6))
        self.text_tr_in = ScrolledText(tab_tr, height=6, wrap="word", font=FONT_MONO,
                                       relief="solid", bd=1, bg="#ffffff")
        self.text_tr_in.pack(fill="x")
        row = tk.Frame(tab_tr, bg=PANEL_BG)
        row.pack(fill="x", pady=6)
        self.btn_translate = tk.Button(
            row, text="翻译为中文", command=self.translate_text, bg=ACCENT, fg="white",
            activebackground=ACCENT_DARK, activeforeground="white", relief="flat",
            font=FONT_BOLD, padx=16, pady=6, cursor="hand2", bd=0)
        self.btn_translate.pack(side="left")
        tk.Button(row, text="复制译文", command=lambda: self.copy_widget(self.text_tr_out),
                  bg="#e2e8f0", fg="#334155", relief="flat", font=FONT_UI,
                  padx=12, pady=6, cursor="hand2", bd=0).pack(side="left", padx=8)
        self.text_tr_out = ScrolledText(tab_tr, height=12, wrap="word", font=FONT_UI,
                                        relief="solid", bd=1, bg="#ffffff")
        self.text_tr_out.pack(fill="both", expand=True, pady=(2, 0))

        # Tab 4 历史记录（SQLite 持久化，越晚生成的越靠前）
        self._build_history_tab(nb)

        # Tab 5 原始返回
        tab_raw = tk.Frame(nb, bg=PANEL_BG, padx=10, pady=8)
        nb.add(tab_raw, text="原始返回")
        self.text_raw = ScrolledText(tab_raw, wrap="word", font=("Consolas", 9),
                                     relief="solid", bd=1, bg="#0f172a", fg="#cbd5e1",
                                     insertbackground="#cbd5e1")
        self.text_raw.pack(fill="both", expand=True)
        self.idx_raw = nb.index(tab_raw)

    # ---------------- 历史记录（SQLite 持久化） ----------------
    def _build_history_tab(self, nb):
        tab = tk.Frame(nb, bg=PANEL_BG, padx=10, pady=8)
        nb.add(tab, text="历史记录")
        self.idx_history = nb.index(tab)

        bar = tk.Frame(tab, bg=PANEL_BG)
        bar.pack(fill="x", pady=(0, 6))
        for text, cmd, colors in (
                ("刷新", self.history_refresh, {}),
                ("载入到「提示词」页", self.history_load_into_prompt, {}),
                ("删除选中", self.history_delete, {}),
                ("清空历史", self.history_clear, {"bg": "#fee2e2", "fg": "#b91c1c"})):
            opts = {"bg": "#e2e8f0", "fg": "#334155"}
            opts.update(colors)
            tk.Button(bar, text=text, command=cmd, relief="flat", font=FONT_UI,
                      padx=12, pady=4, cursor="hand2", bd=0, **opts).pack(
                side="left", padx=(0, 8))
        self.hist_stats = ttk.Label(bar, text="", style="Hint.TLabel")
        self.hist_stats.pack(side="right")

        body = tk.Frame(tab, bg=PANEL_BG)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=0)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        # 左列：图片缩略图（上） + 记录列表（下），越晚生成的越靠前
        left = tk.Frame(body, bg=PANEL_BG)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))

        holder = tk.Frame(left, bg="#eef2f8", height=self.px(200),
                          highlightthickness=1, highlightbackground="#d5dde9")
        holder.pack(fill="x", pady=(0, 6))
        holder.pack_propagate(False)
        self.hist_holder = holder
        self.hist_image = tk.Label(
            holder, text="选中下面的一条记录\n这里显示当时那张图",
            bg="#eef2f8", fg="#9aa8bd", font=FONT_SMALL, justify="center")
        self.hist_image.pack(fill="both", expand=True)

        # 列宽尽量省：模型名在右边详情里已经有，不必再占一列
        cols = ("created_at", "style", "subject")
        heads = {"created_at": "时间", "style": "风格", "subject": "对象"}
        widths = {"created_at": 132, "style": 92, "subject": 56}
        self.hist_tree = ttk.Treeview(
            left, columns=cols, show="headings", selectmode="browse", height=9)
        for c in cols:
            self.hist_tree.heading(c, text=heads[c])
            self.hist_tree.column(c, width=self.px(widths[c]), anchor="w",
                                  stretch=(c == "style"))
        vsb = ttk.Scrollbar(left, orient="vertical",
                            command=self.hist_tree.yview)
        self.hist_tree.configure(yscrollcommand=vsb.set)
        self.hist_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.hist_tree.bind("<<TreeviewSelect>>", self._history_on_select)
        self.hist_tree.bind("<Delete>", lambda e: self.history_delete())

        # 右列：复制按钮 + 文字详情，占满宽度方便读长提示词
        right = tk.Frame(body, bg=PANEL_BG)
        right.grid(row=0, column=1, sticky="nsew")

        row = tk.Frame(right, bg=PANEL_BG)
        row.pack(fill="x", pady=(0, 4))
        for text, cmd in (("复制正向英文", lambda: self.history_copy("positive_en")),
                          ("复制负向英文", lambda: self.history_copy("negative_en")),
                          ("复制整条", self.history_copy_all)):
            tk.Button(row, text=text, command=cmd, bg="#e2e8f0", fg="#334155",
                      relief="flat", font=FONT_SMALL, padx=10, pady=3,
                      cursor="hand2", bd=0).pack(side="left", padx=(0, 6))

        self.hist_text = ScrolledText(right, wrap="word", font=FONT_UI, height=12,
                                      relief="solid", bd=1, bg="#ffffff", fg="#0f172a",
                                      spacing1=2, spacing3=2, padx=6, pady=4)
        self.hist_text.pack(fill="both", expand=True)
        self.hist_text.configure(state="disabled")

        if self.hist_error:
            self.hist_stats.configure(
                text=f"历史记录不可用：{self.hist_error}", foreground="#b91c1c")

    def _on_tab_changed(self, _event=None):
        """切到「历史记录」页时重画一次缩略图。

        没显示的标签页里图片框量到的宽高是 1×1（Tk 不映射它），所以只按启动时算出的
        尺寸画图，窗口窄的时候会被裁边；等这一页真正显示出来再按实际大小画一次。
        """
        if not self._hist_rec:
            return
        try:
            if self.nb.index(self.nb.select()) != self.idx_history:
                return
            self.root.update_idletasks()      # 先让新页完成布局，才量得到真实尺寸
            blob, _mime = history_store.get_image(self._hist_rec["id"])
        except Exception:
            return
        self._history_show_image(blob)

    def _selected_history_id(self):
        sel = self.hist_tree.selection()
        try:
            return int(sel[0]) if sel else None
        except (TypeError, ValueError):
            return None

    def history_refresh(self, select_id=None):
        """重读一遍历史：越晚生成的排越前面。select_id 用来选中刚存进去的那条。"""
        if self.hist_error:
            return
        previous = self._selected_history_id()
        try:
            records = history_store.list_records()
            info = history_store.stats()
        except Exception as e:
            self.hist_error = f"{type(e).__name__}: {e}"
            self.hist_stats.configure(text=f"读历史记录失败：{self.hist_error}",
                                      foreground="#b91c1c")
            return

        self.hist_tree.delete(*self.hist_tree.get_children())
        for r in records:
            self.hist_tree.insert("", "end", iid=str(r["id"]), values=(
                r.get("created_at") or "", r.get("style") or "",
                r.get("subject") or "", r.get("model") or ""))

        mb = info["bytes"] / (1024 * 1024)
        extra = f" · 图片共 {mb:.1f} MB" if info["bytes"] else ""
        self.hist_stats.configure(
            text=f"共 {info['count']} 条{extra} · 数据库：{os.path.basename(info['db_file'])}",
            foreground=MUTED)

        target = str(select_id) if select_id else (
            str(previous) if previous else "")
        if not target and records:
            target = str(records[0]["id"])       # 默认看最新一条（倒序后就是第一行）
        if target and self.hist_tree.exists(target):
            self.hist_tree.selection_set(target)
            self.hist_tree.focus(target)
            self.hist_tree.see(target)
            # 不依赖 <<TreeviewSelect>> 什么时候派发（启动时窗口刚建好，事件还没到），
            # 主动刷一次详情，保证列表和右边永远同步
            self._history_on_select()
        else:
            self._history_show_record(None)

    def _history_on_select(self, _event=None):
        rid = self._selected_history_id()
        if not rid:
            self._history_show_record(None)
            return
        try:
            rec = history_store.get_record(rid)
            blob, _mime = history_store.get_image(rid)
        except Exception as e:
            self.hist_stats.configure(
                text=f"读这条记录失败：{e}", foreground="#b91c1c")
            return
        self._history_show_record(rec, blob)

    def _history_show_record(self, rec, blob=None):
        self._hist_rec = rec
        self.hist_text.configure(state="normal")
        self.hist_text.delete("1.0", "end")
        if rec:
            self.hist_text.insert("1.0", format_record_text(rec))
        else:
            self.hist_text.insert(
                "1.0", "还没有历史记录。\n\n每生成一次提示词，就会把当时那张图和结果存进 "
                       f"{os.path.basename(self.db_file)}，越晚生成的记录排越前面。")
        self.hist_text.configure(state="disabled")
        self._history_show_image(blob if rec else None)

    def _history_show_image(self, blob):
        self._hist_photo = None
        if not blob:
            self.hist_image.configure(image="", text="（这条记录没有图片）")
            return
        # 按图片框的实际大小缩放，窗口拉大拉小都不会把图裁掉一角
        avail_w, avail_h = self.hist_holder.winfo_width(), self.hist_holder.winfo_height()
        if avail_w < 60 or avail_h < 60:       # 还没布局好时给个兜底尺寸
            avail_w, avail_h = self.px(330), self.px(190)
        try:
            im = Image.open(io.BytesIO(blob))
            im.load()
            im.thumbnail((max(1, avail_w - self.px(12)),
                          max(1, avail_h - self.px(12))), Image.LANCZOS)
            self._hist_photo = ImageTk.PhotoImage(im)
        except Exception as e:
            self.hist_image.configure(image="", text=f"图片打不开：{e}")
            return
        self.hist_image.configure(image=self._hist_photo, text="")

    def history_copy(self, field):
        rec = self._hist_rec or {}
        text = (rec.get(field) or "").strip()
        if not text:
            self.set_status("这条记录里这一项是空的")
            return
        self._copy_text(text)

    def history_copy_all(self):
        if not self._hist_rec:
            self.set_status("先在列表里选一条记录")
            return
        self._copy_text(format_record_text(self._hist_rec))

    def history_delete(self):
        rid = self._selected_history_id()
        if not rid:
            self.set_status("先在列表里选一条记录")
            return
        when = (self._hist_rec or {}).get("created_at") or f"id={rid}"
        if not messagebox.askyesno(
                "删除这条记录",
                f"确定删除 {when} 这条记录吗？\n图片和提示词会一起删掉，无法恢复。"):
            return
        try:
            removed = history_store.delete_record(rid)
        except Exception as e:
            messagebox.showerror("删除失败", str(e))
            return
        self.set_status("已删除这条记录" if removed else "这条记录已经不在了")
        self.history_refresh()

    def history_clear(self):
        try:
            total = history_store.stats()["count"]
        except Exception as e:
            messagebox.showerror("读历史记录失败", str(e))
            return
        if not total:
            self.set_status("历史记录本来就是空的")
            return
        if not messagebox.askyesno(
                "清空历史记录",
                f"确定删除全部 {total} 条记录吗？\n图片和提示词会一起删掉，无法恢复。"):
            return
        try:
            removed = history_store.clear_all()
        except Exception as e:
            messagebox.showerror("清空失败", str(e))
            return
        self.set_status(f"已清空 {removed} 条历史记录")
        self.history_refresh()

    def history_load_into_prompt(self):
        rec = self._hist_rec
        if not rec:
            self.set_status("先在列表里选一条记录")
            return
        for widget, key in ((self.text_pos_en, "positive_en"),
                            (self.text_pos_zh, "positive_zh"),
                            (self.text_neg_en, "negative_en"),
                            (self.text_neg_zh, "negative_zh"),
                            (self.text_tips, "tips_zh")):
            widget.delete("1.0", "end")
            widget.insert("1.0", rec.get(key) or "")
        self._last = {k: rec.get(k) or "" for k in
                      ("positive_en", "negative_en", "positive_zh",
                       "negative_zh", "tips_zh")}
        self._last["json_ok"] = bool(rec.get("json_ok"))
        self._last["image_path"] = rec.get("image_name") or ""
        self.nb.select(self.idx_prompt)
        if self._show_history_image(rec):
            self.set_status("已把这条历史记录填回「提示词」页，图片也放到左边了")
        else:
            self.set_status("已填回提示词，但这条记录没有可用的图片")

    def _show_history_image(self, rec) -> bool:
        """把历史记录里存的那张图放到左边预览框。

        用库里的 JPEG 而不是原始文件路径：那条记录可能是几个月前的图，文件早被删了、
        改名了或者换了电脑。顺带把 image_path 清空，避免「预览是历史图、生成却用上次
        选的那张文件」这种错位——generate() 会改用留着的字节。
        """
        try:
            blob, _mime = history_store.get_image(rec["id"])
        except Exception as e:
            self.clear_image()      # 别留着一张和当前提示词无关的旧图
            self.set_status(f"读历史图片失败：{e}")
            return False
        if not blob:
            self.clear_image()
            return False
        try:
            im = Image.open(io.BytesIO(blob))
            im.load()
            thumb = im.copy()
            thumb.thumbnail((self.px(352), self.px(352)), Image.LANCZOS)
            self._photo = ImageTk.PhotoImage(thumb)
        except Exception as e:
            self.clear_image()
            self.set_status(f"这张历史图片打不开：{e}")
            return False

        self._history_image_blob = blob
        self._history_image_name = rec.get("image_name") or ""
        self.image_path = None          # 不是磁盘文件，没路径可记，也不该动"上次目录"
        self.image_size = im.size
        self.preview.configure(image=self._photo, text="")
        self.info.configure(
            text=f"历史记录 #{rec['id']}：{self._history_image_name or '（无文件名）'}\n"
                 f"{im.size[0]} × {im.size[1]} px    {len(blob) / 1024.0:.0f} KB"
                 f"    （库里存的压缩图）")
        return True

    def _block(self, parent, caption, font, height=5):
        """一块「说明 + 复制按钮 + 文本域」，返回 (frame, text, button)。"""
        frame = tk.Frame(parent, bg=PANEL_BG)
        head = tk.Frame(frame, bg=PANEL_BG)
        head.pack(fill="x", pady=(0, 3))
        tk.Label(head, text=caption, bg=PANEL_BG, fg="#475569",
                 font=FONT_SMALL).pack(side="left")
        btn = tk.Button(head, text="复制", bg="#e2e8f0", fg="#334155", relief="flat",
                        font=("Microsoft YaHei UI", 8), padx=8, pady=1, cursor="hand2", bd=0)
        btn.pack(side="right")
        txt = ScrolledText(frame, height=height, wrap="word", font=font,
                           relief="solid", bd=1, bg="#ffffff", fg="#0f172a",
                           insertbackground="#0f172a", spacing1=2, spacing3=2, padx=6, pady=4)
        txt.pack(fill="both", expand=True)
        btn.configure(command=lambda: self.copy_widget(txt))
        return frame, txt, btn

    def _grid_block(self, parent, row, col, caption, font):
        """表格里的一块：左列英文、右列中文，上行正向、下行负向。"""
        frame, txt, btn = self._block(parent, caption, font, 5)
        frame.grid(row=row, column=col, sticky="nsew",
                   padx=(0, 5) if col == 0 else (5, 0),
                   pady=(0, 6) if row == 0 else (6, 0))
        return txt, btn

    def _text_block(self, parent, caption, height, font, weight):
        frame, txt, btn = self._block(parent, caption, font, height)
        frame.pack(fill="both", expand=True, pady=(0, 8))
        return txt, btn

    # ---------------- 图片 ----------------
    def _initial_image_dir(self) -> str:
        """选图对话框从哪个文件夹开始：上次用过的 → 系统图片 → 桌面 → 当前目录。"""
        home = os.path.expanduser("~")
        for cand in (self._last_image_dir,
                     os.path.join(home, "Pictures"),
                     os.path.join(home, "Desktop")):
            if cand and os.path.isdir(cand):
                return cand
        return ""

    def _remember_image_dir(self, path):
        """记住这张图所在的文件夹，下次选图直接从这儿开始。

        「上次选图目录」原来完全靠 Tk / Windows 自己记（跟启动用的解释器、进程工作
        目录都有关系，换个启动方式就会变），所以这里自己存一份（进 history.db 的
        settings 表），行为才稳定。
        """
        folder = os.path.dirname(os.path.abspath(path)) if path else ""
        if not folder or not os.path.isdir(folder):
            return
        # 剪贴板粘出来的图在 %TEMP% 里，记它没意义，还会把下次的对话框带进临时目录
        try:
            if os.path.normcase(folder) == os.path.normcase(tempfile.gettempdir()):
                return
        except Exception:
            pass
        self._last_image_dir = folder
        if self.hist_error:        # 库不可用时就只在内存里记着，不影响选图
            return
        try:
            history_store.set_setting("last_image_dir", folder)
        except Exception:
            pass

    def choose_image(self):
        kwargs = {
            "title": "选择一张图片",
            "filetypes": [("图片", "*.png *.jpg *.jpeg *.webp *.bmp *.gif *.tif *.tiff"),
                          ("所有文件", "*.*")],
        }
        start = self._initial_image_dir()
        # if start:
        #     kwargs["initialdir"] = start
        path = filedialog.askopenfilename(**kwargs)
        if path:
            self.load_path(path)

    def load_path(self, path):
        try:
            im = load_image(path)
        except Exception as e:
            messagebox.showerror("打不开这张图片", f"{path}\n\n{e}")
            return
        self.image_path = path
        self.image_size = im.size
        self._history_image_blob = None      # 换了张真文件，历史那张就不算了
        self._history_image_name = ""
        thumb = im.copy()
        thumb.thumbnail((self.px(352), self.px(352)), Image.LANCZOS)
        self._photo = ImageTk.PhotoImage(thumb)
        self.preview.configure(image=self._photo, text="")
        size_kb = os.path.getsize(path) / 1024.0 if os.path.exists(path) else 0
        self.info.configure(
            text=f"{os.path.basename(path)}\n{im.size[0]} × {im.size[1]} px"
            f"    {size_kb:.0f} KB    {im.mode}")
        self.set_status(f"已选择：{os.path.basename(path)}")
        self._remember_image_dir(path)

    def paste_image(self):
        try:
            clip = ImageGrab.grabclipboard()
        except Exception as e:
            messagebox.showerror("剪贴板不可用", str(e))
            return
        if clip is None:
            messagebox.showinfo(
                "剪贴板里没有图片", "请先复制一张图片（截图或右键复制），再按 Ctrl+V。")
            return
        if isinstance(clip, list):
            for p in clip:
                if os.path.splitext(p)[1].lower() in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"):
                    self.load_path(p)
                    return
            messagebox.showinfo("剪贴板里没有图片", "复制的内容不是图片文件。")
            return
        fd, tmp = tempfile.mkstemp(prefix="clipboard_", suffix=".png")
        os.close(fd)
        clip.save(tmp)
        self.load_path(tmp)

    def _on_ctrl_v(self, _event=None):
        # 焦点在输入框/文本框里时，Ctrl+V 交给控件自己粘贴文字；否则当作粘贴图片
        focused = self.root.focus_get()
        if isinstance(focused, (tk.Entry, tk.Text, ttk.Entry, ttk.Combobox)):
            return
        self.paste_image()

    def clear_image(self):
        self.image_path = None
        self.image_size = None
        self._photo = None
        self._history_image_blob = None
        self._history_image_name = ""
        self.preview.configure(
            image="", text="点击这里选择图片\n\n或按 Ctrl+O\n也可以复制截图后按 Ctrl+V")
        self.info.configure(text="尚未选择图片")

    # ---------------- 生成 ----------------
    def generate(self):
        if self.busy:
            return
        if not (self.image_path or self._history_image_blob):
            messagebox.showwarning(
                "还没有选图片",
                "请先点左边的图片框选一张图，或按 Ctrl+O。\n"
                "也可以在「历史记录」页选一条记录，点「载入到「提示词」页」。")
            return
        if not config_ready():
            messagebox.showwarning("还没配置 API", config_help_text())
            return

        path = self.image_path
        history_blob = self._history_image_blob
        history_name = self._history_image_name
        style = self.style_var.get()
        subject = self.subject_var.get()
        extra = self.extra_var.get()
        self._set_busy(True, "正在分析图片并生成提示词…")
        self.nb.select(self.idx_prompt)

        def job():
            if history_blob:
                # 历史里的图已经是压缩好的 JPEG，直接用，不必再压一遍（省一次有损编码）
                raw = history_blob
                im = Image.open(io.BytesIO(raw))
                im.load()
                image_name = history_name or "history.jpg"
            else:
                im = load_image(path)
                raw = prepare_upload_bytes(im)
                image_name = os.path.basename(path)
            b64 = base64.b64encode(raw).decode("ascii")
            text, info = ask_vision(build_messages(b64, style, extra, subject))
            data = parse_result(text)
            payload = {"text": text, "info": info, "data": data,
                       "style": style, "subject": subject,
                       "record_id": None, "store_error": ""}
            # 存历史放在同一个后台线程里：界面不用等，存失败也不影响这次生成的结果
            try:
                payload["record_id"] = history_store.save_record(
                    image_bytes=raw,
                    image_name=image_name,
                    image_w=im.size[0], image_h=im.size[1],
                    style=style, subject=subject, extra=extra,
                    model=info.get("model") or MODEL,
                    positive_en=data["positive_en"], negative_en=data["negative_en"],
                    positive_zh=data["positive_zh"], negative_zh=data["negative_zh"],
                    tips_zh=data["tips_zh"], json_ok=data["json_ok"],
                    elapsed=info["elapsed"], tokens_total=usage_total(
                        info.get("usage")),
                )
            except Exception as e:      # noqa: BLE001 - 存历史失败不能毁掉这次生成
                payload["store_error"] = f"{type(e).__name__}: {e}"
            return payload

        self._run_async(job, "gen")

    def translate_text(self):
        if self.busy:
            return
        src = self.text_tr_in.get("1.0", "end-1c").strip()
        if not src:
            messagebox.showinfo("先输入点内容", "把英文提示词粘贴到上面的输入框里。")
            return
        if not config_ready():
            messagebox.showwarning("还没配置 API", config_help_text())
            return
        self._set_busy(True, "正在翻译…")

        def job():
            text, info = ask_translate(src)
            return {"text": text, "info": info}

        self._run_async(job, "translate")

    def _run_async(self, fn, tag):
        def runner():
            try:
                self.q.put({"kind": "ok", "tag": tag, "payload": fn()})
            except BaseException as e:      # noqa: BLE001 - 统一转成友好提示
                self.q.put({"kind": "err", "tag": tag,
                           "msg": humanize_api_error(e)})
        threading.Thread(target=runner, daemon=True).start()

    # ---------------- 队列 / 状态 ----------------
    def _drain(self):
        try:
            while True:
                self._handle(self.q.get_nowait())
        except queue.Empty:
            pass
        self.root.after(120, self._drain)

    def _handle(self, msg):
        tag = msg.get("tag")
        if msg["kind"] == "err":
            self._set_busy(False, "出错了")
            self._append_raw(f"[{tag}] 请求失败\n\n{msg['msg']}\n\n"
                             f"BASE_URL = {BASE_URL}\nMODEL    = {MODEL}")
            self.nb.select(self.idx_raw)
            messagebox.showerror(
                "调用失败", msg["msg"] + f"\n\n（BASE_URL = {BASE_URL}，MODEL = {MODEL}）")
            return

        payload = msg["payload"]
        if tag == "translate":
            self.text_tr_out.delete("1.0", "end")
            self.text_tr_out.insert("1.0", payload["text"])
            self._set_busy(
                False, f"翻译完成 · 用时 {payload['info']['elapsed']:.1f} s")
            return

        # tag == "gen"
        data = payload["data"]
        self._last = data
        self.text_pos_en.delete("1.0", "end")
        self.text_pos_en.insert("1.0", data["positive_en"])
        self.text_pos_zh.delete("1.0", "end")
        self.text_pos_zh.insert("1.0", data["positive_zh"])
        self.text_neg_en.delete("1.0", "end")
        self.text_neg_en.insert("1.0", data["negative_en"])
        self.text_neg_zh.delete("1.0", "end")
        self.text_neg_zh.insert("1.0", data["negative_zh"])
        self.text_tips.delete("1.0", "end")
        self.text_tips.insert("1.0", data["tips_zh"])

        info = payload["info"]
        json_mode = info.get("json_mode", True)
        mode_text = ("response_format=json_object" if json_mode
                     else "该接口不支持 response_format，已去掉参数重试（纯文本模式）")
        record_id = payload.get("record_id")
        if record_id:
            hist_text = f"已存入历史记录（#{record_id}）"
        elif payload.get("store_error"):
            hist_text = f"没存上：{payload['store_error']}"
        else:
            hist_text = "—"
        if record_id:
            self.history_refresh(select_id=record_id)
        self._append_raw(
            f"请求成功 · 风格「{payload['style']}」 · 描述对象「{payload['subject']}」\n"
            f"BASE_URL : {BASE_URL}\n"
            f"MODEL    : {info['model']}\n"
            f"耗时     : {info['elapsed']:.2f} s\n"
            f"tokens   : {usage_text(info['usage'])}\n"
            f"JSON 模式 : {mode_text}\n"
            f"JSON 解析 : {'成功' if data['json_ok'] else '失败（已兜底显示）'}\n"
            f"历史记录 : {hist_text}\n"
            f"{'-' * 60}\n模型返回原文：\n{payload['text']}\n")

        note = "完成" if data["json_ok"] else "完成（模型未按 JSON 返回，已兜底）"
        if not json_mode:
            note += " · 已降级为纯文本模式"
        if record_id:
            note += " · 已存历史记录"
        self._set_busy(False, f"{note} · 用时 {info['elapsed']:.1f} s")

    def _set_busy(self, busy, text):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for b in (self.btn_generate, self.btn_save, self.btn_clear, self.btn_translate):
            b.configure(state=state, cursor="arrow" if busy else "hand2")
        self.combo.configure(state="disabled" if busy else "readonly")
        self.subject_combo.configure(state="disabled" if busy else "readonly")
        self.entry.configure(state=state)
        if busy:
            self.progress.configure(mode="indeterminate")
            self.progress.start(14)
            self._t0 = time.time()
            self._tick()
        else:
            self.progress.stop()
            # indeterminate 模式下 value 不生效，切回 determinate 才能真正清空那截绿条
            self.progress.configure(mode="determinate", value=0)
        self.set_status(text)

    def _tick(self):
        if not self.busy:
            return
        cur = self.status.cget("text")
        base = cur.split("   ")[0]
        self.status.configure(text=f"{base}   {time.time() - self._t0:.1f}s")
        self.root.after(200, self._tick)

    def set_status(self, text):
        self.status.configure(text=text)

    def _append_raw(self, text):
        self.text_raw.delete("1.0", "end")
        self.text_raw.insert("1.0", text)

    # ---------------- 复制 / 保存 ----------------
    def _copy_text(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update_idletasks()
        self.set_status(f"已复制 {len(text)} 个字符到剪贴板")

    def copy_widget(self, widget):
        txt = widget.get("1.0", "end-1c").strip()
        if not txt:
            self.set_status("没有可复制的内容")
            return
        self._copy_text(txt)

    def clear_result(self):
        for w in (self.text_pos_en, self.text_pos_zh, self.text_neg_en, self.text_neg_zh,
                  self.text_tips, self.text_raw, self.text_tr_out):
            w.delete("1.0", "end")
        self._last = {}
        self.set_status("已清空结果")

    def save_result(self):
        body = self.build_export_text()
        if not body.strip():
            messagebox.showinfo("还没有结果", "先生成一次提示词，再保存。")
            return
        path = filedialog.asksaveasfilename(
            title="保存提示词", defaultextension=".txt",
            initialfile=time.strftime("prompt_%Y%m%d_%H%M%S.txt"),
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(body)
        except Exception as e:
            messagebox.showerror("保存失败", str(e))
            return
        self.set_status(f"已保存到 {path}")

    def build_export_text(self):
        d = self._last
        if not d:
            return ""
        lines = [
            "ComfyUI 提示词助手的生成结果",
            f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"图片：{d.get('image_path') or self.image_path}",
            f"模型：{MODEL}",
            f"描述对象：{self.subject_var.get()}    风格：{self.style_var.get()}",
            "",
            "【正向提示词 · English】",
            d.get("positive_en", ""),
            "",
            "【正向提示词 · 中文】",
            d.get("positive_zh", ""),
            "",
            "【负向提示词 · English】",
            d.get("negative_en", ""),
            "",
            "【负向提示词 · 中文】",
            d.get("negative_zh", ""),
            "",
            "【提示词解读】",
            d.get("tips_zh", ""),
            "",
        ]
        return "\n".join(lines)


def main():
    try:  # 高分屏不模糊
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    PromptApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
