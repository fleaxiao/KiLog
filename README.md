# KiLog

KiLog 是面向 KiCad 9/10 PCB Editor 的 IPC 扩展。它直接轮询编辑器内存中的 `BOARD` 对象，因此器件移动/旋转、走线、过孔、铺铜等操作尚未保存到原始 `.kicad_pcb` 文件时，也能被记录。

## UI 与行为

- `start`：锁定两个文件名前缀并开始监听。默认 PCB 快照名为 `ref`，日志名为 `log`。
- `note`：先写入待处理变化，再通过 KiCad 的 Save Copy API 保存 `ref_000001.kicad_pcb`；不会改变当前编辑文档的文件名。
- `undo`：优先执行一次 KiCad 原生撤销，并删除相应的 `log_XXXXXX.json`。若原生撤销后的对象状态与该日志事件起点不一致，则从内存对象快照精确恢复。
- `end`：立即写入最后一批变化并结束监听。

输出目录是当前 PCB 文件所在目录。每个日志事件、每个字段变化和每个 KiCad 对象分别包含 `event_uuid`、`change_uuid` 和 `item_uuid`。日志使用 JSON Patch 风格的 `add` / `remove` / `replace` 操作，并带有语义分类和前后状态指纹。

```json
{
  "event_uuid": "…",
  "sequence": 1,
  "summary": {"operations": {"footprint.move": 2}},
  "changes": [
    {
      "change_uuid": "…",
      "item_uuid": "…",
      "operation": "footprint.move",
      "op": "replace",
      "path": "/items/…/data/position/x",
      "before": "10000000",
      "after": "10500000"
    }
  ]
}
```

## 安装

首选方式是在 KiCad 的 Plugin and Content Manager 中从文件安装 [kilog-pcm-1.0.1.zip](dist/kilog-pcm-1.0.1.zip)。手动安装时，解压 [kilog-plugin-1.0.1.zip](dist/kilog-plugin-1.0.1.zip)，把其中的 `kilog` 文件夹放到：

- Windows：`%USERPROFILE%\Documents\KiCad\<版本>\plugins\kilog`
- macOS：`~/Documents/KiCad/<版本>/plugins/kilog`
- Linux：`~/.local/share/KiCad/<版本>/plugins/kilog`

在 KiCad 的 `Preferences > Plugins` 中启用 IPC API，重新加载插件。首次加载会为插件建立 Python 环境并安装 `kicad-python`。随后从 PCB Editor 工具栏点击 KiLog 图标。

UI 使用 KiCad 自带的 wxPython，不依赖 `_tkinter`。

## 设计说明

正式图标以黄色折角日志文件为主体，把 PCB 走线和焊盘直接融入页面，颜色延续深绿 `#063D2C`、黄橙 `#F5A11A` 与奶白 `#F7F2E8`。ImageGen 母版位于 [kilog-logo-imagegen.png](design/generated/kilog-logo-imagegen.png)，正式 SVG 位于 [icon.svg](plugin/assets/icon.svg)，各尺寸 PNG 由 `tools/render_icons.py` 统一生成。

## 开发与验证

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\kicad-python-packager.exe validate plugin
.\.venv\Scripts\python.exe tools\build_package.py
.\.venv\Scripts\kicad-python-packager.exe validate dist\kilog-pcm-1.0.1.zip
```

IPC API 当前是同步请求/响应接口，没有异步编辑事件，因此 KiLog 以 160 ms 周期读取对象，并在 450 ms 稳定窗口后把连续拖动合并为一次操作。路由器或其他交互工具返回 busy 时，记录器会等待工具完成。`undo` 只撤销当前 KiLog 会话内已经写入日志的操作。
