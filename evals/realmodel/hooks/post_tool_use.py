"""异步钩子夹具：`post_tool_use`，退出码 2 唤醒模型。

正文按 **GBK** 写 stderr（不是本机默认编码，是钉死的），因为这条夹具要测的就是
「外部程序按本机码页写字节」这一常态——换一台 UTF-8 码页的机器，若跟着 locale 写
就变成纯 UTF-8，产品的解码退路当场失去被测对象。逐字正文在 `wake-text.txt`，
产品侧解码逻辑见 `services/api/src/blank_api/services/hooks.py::_decode`。
"""

import json
import sys
from pathlib import Path

sys.stdin.reconfigure(encoding="utf-8")
payload = json.load(sys.stdin)
if payload.get("tool") != "read_file":
    sys.exit(0)

text = (Path(__file__).resolve().parent.parent / "fixtures" / "wake-text.txt")
wake = text.read_text(encoding="utf-8").rstrip("\r\n")
sys.stderr.buffer.write(wake.encode("gbk", errors="strict"))
sys.stderr.buffer.flush()
sys.exit(2)
