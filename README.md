# ComfyUI 提示词助手

给一张图，调用视觉大模型生成**英文提示词**，同时给出**中文翻译**和**提示词解读**。
纯 Python + tkinter，界面只用标准库。项目带 uv 配置：`uv sync` 生成 `.venv` 后，双击 `.bat` 默认用这个虚拟环境启动（依赖版本和 `uv.lock` 一致）；万一还没有 `.venv`，会提示并退回全局 python。

图片上传前会压成 JPEG（长边 ≤ 1280），一张图约 20 KB，省 token。
每次生成都会连图片一起存进本地 SQLite（`history.db`），「历史记录」页可以按时间回看，最新的排最前面。

## 项目结构

```
comfyui-prompt-helper/
├── main.py                    # 入口，转调 comfyui_prompt_helper.main()
├── comfyui_prompt_helper.py   # 程序本体：界面 + 调模型 + 解析
├── history_store.py           # 历史记录的 SQLite 存取（纯数据层，不含界面代码）
├── comfyui_prompt_helper.bat  # 双击启动（优先 .venv\Scripts\pythonw.exe，没有才用全局 python）
├── .env                       # 你的 Key / 地址 / 模型名（不进 git）
├── .env.example               # 模板，复制成 .env 再填
├── history.db                 # 历史记录数据库（首次生成时自动创建，不进 git）
├── README.md
├── pyproject.toml             # uv / pip 的项目描述（依赖：openai、pillow、python-dotenv）
└── uv.lock
```

## 第一步：配 .env（必须做）

把 `.env.example` 复制成 `.env`（项目目录下），填三行：

```ini
API_KEY=sk-xxxxxxxxxxxxxxxxxxxx
BASE_URL=https://api.deepseek.com
MODEL=deepseek-flash
```

- `.env` 已在 `.gitignore` 里，不会进 git；
- 改完 `.env` 要**重启程序**才生效（Key 在启动时读一次）；
- `.env` 没写某一项时，会退回 `comfyui_prompt_helper.py` 顶部【配置区】的内置默认值；
- 程序标题栏右侧会显示当前模型、接口地址和配置来源（`.env` 或 `内置默认值`），可以据此确认有没有读到。

`MODEL` **必须选支持图片输入的视觉模型**。常用服务写法：

| 服务 | BASE_URL | MODEL |
| --- | --- | --- |
| DeepSeek | `https://api.deepseek.com` | `deepseek-flash`（实测支持图片输入） |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4.5v` / `glm-4v-flash`（免费） |
| 阿里云百炼 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-vl-max` |
| 月之暗面 | `https://api.moonshot.cn/v1` | `moonshot-v1-8k-vision-preview` |
| 火山方舟 | `https://ark.cn-beijing.volces.com/api/v3` | `doubao-1.5-vision-pro-32k` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` / `gpt-4o` |

`BASE_URL` 只写到 `/v1`、`/v3`、`/v4` 这一层，**不要**自己补 `/chat/completions`。

## 第二步：跑起来

任选一种：

```powershell
# 1) 推荐：双击 comfyui_prompt_helper.bat
#    内部用项目 .venv 里的 pythonw 启动，不出黑框

# 2) 命令行等价写法（第一次先同步依赖，生成 .venv）
uv sync
uv run main.py

# 3) 直接用虚拟环境里的解释器
.venv\Scripts\python.exe main.py

# 4) 全局 python（跳过虚拟环境，需要自己 pip install pillow openai，
#    且 openai 版本要 >= 3.37）
python main.py
```

> `.bat` 的查找顺序是 `.venv\Scripts\python.exe` → 全局 `python`。
> 项目里还没有 `.venv` 时它会先提示一句再退回全局 python，所以想省事就先跑一次 `uv sync`。

### 用法

1. 点左边的图片框选一张图（`Ctrl+O` 也行；截图后按 `Ctrl+V` 可直接粘贴，右键图片框也有菜单）；
2. 选「描述对象」（整张图片 / 人物 / 背景）和「提示词风格」，需要时填「补充要求」（比如"偏动漫一点，加雪景"）；
3. 点「生成提示词」（`Ctrl+Enter`）；
4. 在「提示词」页点「复制」拿左边的英文，粘进 ComfyUI 的正向 / 负向框；右边的中文只是给你看的，别粘。
5. 想找以前生成过的结果，切到「历史记录」页。

窗口右上角有个「置顶窗口」按钮：点一下窗口就始终浮在别的程序上面（按钮变成蓝色的「已置顶」），方便一边看提示词一边操作 ComfyUI；再点一次取消。默认不置顶，也不记忆到下次启动。

### 五个标签页

- **提示词**：2×2 对照表——左列英文、右列中文，上行正向、下行负向。四块各有一个「复制」按钮
- **提示词解读**：中文说明模型为什么这么写，适合学习
- **翻译工具**：随便贴一段英文提示词（别人的工作流里抄来的也行），逐条翻译成中文
- **历史记录**：SQLite 里存着每次生成的图片和提示词，**最新的排最前面**（见下一节）
- **原始返回**：模型原文 + 耗时 / token / 是否成功解析，出错时先看这里

## 历史记录

每次点「生成提示词」成功拿到结果后，程序会把这一次**原样存档**：

- 那次上传用的压缩 JPEG（就是真正发给模型的那张，长边 ≤ 1280）；
- 正向 / 负向提示词的英文和中文、提示词解读；
- 当时的描述对象、提示词风格、补充要求、模型名、耗时、token 数、JSON 是否解析成功。

存在项目目录下的 `history.db`（SQLite，单文件，已在 `.gitignore` 里），首次生成时自动创建。
界面左边是**缩略图 + 记录列表**，右边是这一条的**完整字段**：

- 列表按时间**倒序**排，**最新生成的就在最上面**，往下翻是越来越老的记录；点一行，上面显示当时的图，右边显示全部字段；
- 「复制正向英文 / 复制负向英文 / 复制整条」把内容送进剪贴板；
- 「载入到「提示词」页」把这条记录填回主界面：四个提示词框和解读一起回来，**图片也回到左边预览框**（用的是库里存的那张压缩图，原文件删了/改名了都不影响）。载入完直接点「生成提示词」，就是拿这张图重跑一遍——换风格、改补充要求都可以，不用重新找图；
- 「删除选中」删一条（也可以按 `Delete` 键），「清空历史」删全部，都会先确认。

几个实际考虑：

- **体积**：一条记录就是一张压缩图 + 一点文字，通常几十到几百 KB；工具栏右侧会显示`共 N 条 · 图片共 X MB`，嫌大就删掉或者直接删 `history.db`；
- **排序**按时间戳，不是按插入顺序，所以改了系统时间也不会乱；
- **列表不读图片**：只有点开某一条才按 id 取图，记录多了不会卡；
- **写库在后台线程**，和生成请求同一个线程，界面不冻结；万一存失败（比如磁盘满了），只会在「原始返回」页写一句`历史记录 : 没存上：…`，**不影响这次生成的结果**；
- **载入后可以重跑**：历史里那张图本身就是压缩好的 JPEG，重新生成时直接用、不再压一遍（不会越跑越糊），新记录沿用原来的文件名；
- **WAL 模式**会额外生成 `history.db-wal` / `history.db-shm`，一并已加进 `.gitignore`；
- 换电脑想带走历史，复制这三个文件（正常退出后主要就是 `history.db`）即可。

## 描述对象

同样是这张图，你想让提示词重点写什么：

- **整张图片（默认）**：主体、环境、光线、构图、风格均衡覆盖
- **人物**：重点写人数、发型发色、瞳色、表情、服装材质、姿态动作、打光与景别，背景一句话带过；画面里没人物时，会挑最像主体的对象来写，并在解读里说明
- **背景**：重点写地点、空间结构、天气时间、光源光影、景深与色调；人物只交代位置

## 提示词风格

- **标签式 · SD1.5 / SDXL（推荐）**：逗号分隔的 Danbooru 风格标签，画质词前置，适合大部分国模
- **自然语言 · SD3 / Flux**：1~3 句连贯英文，不堆标签
- **动漫 / 二次元**：面向 Pony / Illustrious / Animagine
- **写实摄影**：机位、焦段、光源、质感
- **图生图 · 保留原图构图**：只描述内容，不写会改变布局的构图词，配合 img2img / ControlNet
- **技术插画 / 产品图**：棚拍、渲染质感

## 常见问题

**报 400 / 请求被拒绝**：程序会先带 `response_format={"type":"json_object"}` 请求，一旦被 400 拒绝就**自动去掉这个参数重试一次**（有些兼容端点不认它）；两次都 400 才会报错，所以这时基本可以断定是 `MODEL` 不是视觉模型，换 `glm-4.5v`、`qwen-vl-max` 这类带 V 的模型。

**报 404**：`BASE_URL` 写错了。注意别重复写 `/chat/completions`，各家版本号是 `/v1`、`/v3`、`/v4`。

**报 401**：`.env` 里的 `API_KEY` 不对或过期。程序弹窗会把当前读到的 Key 打码显示（形如 `sk-af4…8453`），可以先对照一下是不是读到的那把。

**报 `TypeError: process() takes no keyword arguments`**：这不是 Key 的问题，是本地 `brotli` 1.0.9 太旧——openai 3.x 依赖的 `httpx2` 解 `Content-Encoding: br` 时会调 `brotli.Decompressor.process(data, output_buffer_limit=...)`，旧版不认这个参数。程序已默认发送 `Accept-Encoding: identity` 让服务器不压缩来规避；万一还遇到，`pip install -U brotli` 升级后重启即可。

**状态栏停在"生成中"很久**：视觉模型要看图，通常 5~15 秒，属正常；超过 2 分钟会由超时兜底报错。

**提示"模型未按 JSON 返回，已兜底"**：模型没老老实实输出 JSON。程序仍然会把内容塞进正向提示词框，重试一次通常就好。如果「原始返回」页的「JSON 模式」一行写着"已去掉参数重试（纯文本模式）"，说明该接口不支持 `response_format`，这种时候纯文本返回是正常的，靠 `parse_result()` 的小标题切分兜底。

**中文翻译能用中文提示词框吗？** 不行。ComfyUI 里主流 CLIP 文本编码器只吃英文，中文喂进去基本等于乱码。

**选图对话框总是从「文档」开始，怎么固定到我的图库？** 程序会记住你上次选图所在的文件夹（存在 `history.db` 的 `settings` 表里，键名 `last_image_dir`），下次打开对话框直接从那儿开始；第一次用或换机器时退到系统的「图片」文件夹。想换起点，随便从目标目录选一次图即可（从剪贴板粘的图不会改这个记忆）。

**历史记录越攒越多怎么办？** 列表上方有`共 N 条 · 图片共 X MB`。单条通常几十到几百 KB，想清理就在历史页「删除选中」/「清空历史」，或者直接删掉项目目录下的 `history.db`（下次生成会重新建）。

**历史记录里没有我刚生成的那条？** 只在这两种情况下会缺：一是生成失败（没拿到结果就不存档），二是存库出错——这时「原始返回」页的`历史记录 :`一行会写明原因，结果本身不受影响。

## 实现要点（想改的话）

- `ask_vision()`：OpenAI SDK 的 `client.chat.completions.create`，消息里放 `{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}`。第一次带 `response_format={"type": "json_object"}`；若被 400 拒绝，就原样去掉这个参数重试一次，并把 `info["json_mode"] = False` 标出来，界面「原始返回」页会写明走了降级。
- `parse_result()`：先按 JSON 解析，失败的再按「Positive prompt / 正向提示词」小标题切分，最后兜底整段当正向提示词。
- 接口地址由 `get_client()` 里配的 `BASE_URL` 决定，只喂到 `/v1` `/v3` `/v4` 这一层，其余路径由 SDK 拼。
- 网络请求跑在后台线程，结果通过 `queue.Queue` 回主线程刷界面，所以生成时窗口不会卡死。
- 界面尺寸按系统 DPI 缩放（`self.scale`），高分屏不会糊也不会错位。
- 历史记录全在 `history_store.py` 里，只有四个入口：`save_record()` / `list_records()` / `get_record()`+`get_image()` / `delete_record()`+`clear_all()`，都是「一次操作一个连接」，不长期持有，所以后台线程写、主线程读不会打架（WAL + 模块级锁兜底）。想加字段就改 `SCHEMA_SQL` 和 `LIST_COLUMNS`/`FULL_COLUMNS`，顺手升一下 `SCHEMA_VERSION`。
- `choose_image()` 的起始目录是 `self._initial_image_dir()`：先看 `settings` 表里的 `last_image_dir`，没有就用系统的「图片」/「桌面」。**不要**指望 Tk / Windows 自己记上次目录——它跟启动用的解释器、进程工作目录有关，换个启动方式就变，所以这里显式存了一份。`load_path()` 成功后调 `_remember_image_dir()` 写回（剪贴板临时文件所在的 `%TEMP%` 会被跳过）。
- `prepare_upload_bytes()` 返回的 JPEG 字节同时用于「发给模型」和「存进历史」，所以历史里的图就是当时实际发出去的那张，不额外占空间。

## 注意

Key 只放在 `.env` 里（已在 `.gitignore`），别把 `.env` 发到群里或提交上去。
`comfyui_prompt_helper.py` 现在**不含**任何 Key，可以放心提交。
`history.db` 里存着**你生成过的图片和提示词**，同样在 `.gitignore` 里；分享项目目录时记得别把它一起打包。
