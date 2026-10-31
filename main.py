# -*- coding: utf-8 -*-
"""项目入口。

    python main.py       # 用全局 python
    uv run main.py       # 用项目里的 .venv

真正的程序在 comfyui_prompt_helper.py 里（单文件，界面 + 调用 + 解析都在里面）。
"""

from comfyui_prompt_helper import main

if __name__ == "__main__":
    main()
