# local_pen

local_pen 让 Claude Code、Codex 或其他支持本地 stdio MCP 的客户端直接修改选中的 `.pen`。浏览器实时预览使用 Pen CLI 无界面渲染，可放在副屏，AI 保存后会自动更新。AI 客户端只需连接 `local_pen`。Pen 桌面模式适合把 Pen 留在当前桌面上直接查看。

## 1. 安装

准备 macOS、Python 3.10+、Node.js 22.19+，以及 Claude Code 或 Codex。把仓库克隆到你打算长期使用的目录。

```bash
git clone https://github.com/wuyongli/local_pen.git
cd local_pen
```

运行与你使用的客户端对应的命令。

```bash
bash setup_local_pen.sh claude
bash setup_local_pen.sh codex
bash setup_local_pen.sh both
```

脚本在包内安装 Pen CLI 0.3.16，注册名为 `local_pen` 的服务。重开 AI 客户端后，Claude Code 可通过 `/mcp` 查看连接，Codex CLI 可用 `codex mcp list` 查看。

```bash
bash setup_local_pen.sh check
```

如果预览页显示渲染不可用，先确认 CLI 版本。需要登录时，在同一个目录运行以下命令并登录自己的 Pen 账号。浏览器预览无需打开 Pen 桌面应用，也无需授予辅助功能权限。 [Pen CLI 安装与认证](https://docs.pencil.dev/for-developers/pen-cli)

```bash
bash setup_local_pen.sh login
```

## 2. 用示例试一次

1. 把 `快速体验.pen` 复制到你的任务目录。在 AI 客户端打开该目录，先说「把 `快速体验.pen` 设为本任务设计稿，把标题改成『今天的待办』」。
2. 本次保存会开启实时预览。手动打开返回的链接一次，再让 AI 修改确认按钮。
3. 把标签页留着。后续只需自然描述设计要求，AI 保存后，原标签页会自动显示新稿。需要自查画面时，AI 可用 `preview_png`。

「预览本任务设计图」会显示整个 `.pen` 画布。所有可见画板按原位置排列，包括随内容调整大小的画板。画板上方显示设计稿里的名称，鼠标停在标题上可看全名。触控板双指捏合和右上角的「+」「−」共用画布缩放比例。放大后，双指滑动或按住拖动可把边角内容移到窗口中央。点「适合窗口」恢复整稿视图。自动刷新会保留当前缩放和查看位置。

macOS 副屏预览优先用本机缓存的字体、图标和本地素材渲染矢量画布，放大后文字和线条仍清晰。无法在本机导出矢量图时，会回退到 PNG。整稿仅缺缓存字体时，会提示字体可能有差异。连续保存时会跳过已经被新版本取代的预览。

预览服务独立于 AI 会话。同一任务的会话共用本机预览地址。关掉任一会话后，只要原标签页还在轮询，其他会话保存就会继续更新该页。说「停止实时预览」可主动结束服务。如果浏览器与会话连续 10 分钟都未访问服务，空闲进程会自动退出，下次改稿会恢复预览。旧端口若被其他程序或较早版本的服务占用，会分配可用的新地址。

## 3. 选自己的工作方式

| 使用场景 | 做法 |
| --- | --- |
| 主屏留在 Pen | 在 Pen 中打开任务稿，改稿时明确要求「保存后在 Pen 中重开」。macOS 窗口重开需完成下方的一次性设置。 |
| 主屏工作，副屏看画板 | 首次改稿后，把返回的预览页拖到副屏再手动全屏。后续保存会自动更新该页。 |
| 先看方案再决定 | 请求 AI 先做临时预览。确认后再让 AI 应用到任务稿。 |

多个会话可编辑同一任务稿。保存使用文件锁。提示稿件已更新时，先读取最新图层，再决定如何应用改动。如果该稿也在 Pen 中打开，进行后台改稿前先在 Pen 保存并关闭它，以免窗口中的旧版本覆盖新稿。要回到 Pen 窗口工作流，先说「停止实时预览」，再在 Pen 打开最新稿。初次使用建议选择工作副本。多屏可设为扩展显示，并在 macOS「桌面与程序坞 → 调度中心」启用「显示器具有单独的空间」。 [Apple 调度中心设置](https://support.apple.com/zh-cn/guide/mac-help/mchlp1119/mac)

### 使用 Pen 窗口自动重开

在安装包目录运行以下命令，按提示添加辅助程序。安装浏览器预览的人可跳过此步。

```bash
bash setup_local_pen.sh desktop
```

把目标 `.pen` 打开在普通 Pen 窗口。改稿前保存你在窗口里的手改。独立全屏的 Pen 窗口使用浏览器实时预览模式更合适。

## 4. 升级与其他客户端

如果客户端已指向此仓库，在仓库目录运行 `git pull --ff-only`，然后重启客户端。已有独立安装时，可将仓库里的 Python 文件及 `pen_font_cache.cjs` 更新到现有服务目录，保留已授权的 `pen-reopen`。切换安装位置时，先移除对应客户端的旧注册，再运行安装命令。

```bash
claude mcp remove local_pen -s user
codex mcp remove local_pen
```

其他客户端可添加名为 `local_pen` 的本地 stdio 服务。命令使用 Python 3 的绝对路径，参数是 `-B` 和安装目录中 `local_pen_mcp.py` 的绝对路径。设置 `LOCAL_PEN_NODE` 为 Node 的绝对路径，`LOCAL_PEN_CLI` 为安装目录中 `.runtime/node_modules/.bin/pen` 的绝对路径。

## 5. 开发验证

在仓库根目录运行以下命令。测试使用临时文件和模拟渲染器，无需连接 AI 客户端或登录 Pen。

```bash
python3 -B -m unittest discover -s tests -p 'test_*.py'
node --test tests/test_pen_font_cache.cjs
bash -n setup_local_pen.sh
```
