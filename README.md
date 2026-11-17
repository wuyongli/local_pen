# local_pen

local_pen 让 Claude Code、Codex 或其他支持本地 stdio MCP 的客户端直接修改选中的 `.pen`。浏览器实时预览使用 Pen CLI 的无界面 PNG 渲染，可放在副屏，AI 保存后会自动更新。AI 客户端只需连接 `local_pen`。Pen 桌面模式适合把 Pen 留在当前桌面上直接查看。

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

1. 把 `快速体验.pen` 复制到你的任务目录。在 AI 客户端打开该目录，先说「把 `快速体验.pen` 设为本任务设计稿，开启实时预览，把链接给我。」
2. 手动打开链接一次。页面显示「待办清单」后，回到同一个 AI 会话说「把标题改成『今天的待办』，把确认按钮改成深绿色」。
3. 把标签页留着。后续只需自然描述设计要求，AI 保存后，原标签页会自动显示新稿。需要自查画面时，AI 可用 `preview_png`。

「预览本任务设计图」会显示整个 `.pen` 画布。所有可见画板按原位置排列，包括随内容调整大小的画板。画板上方显示设计稿里的名称，鼠标停在标题上可看全名。触控板双指捏合和右上角的「+」「−」共用画布缩放比例。双指滑动或按住拖动可移动画布，点「适合窗口」恢复整稿视图。自动刷新会保留当前缩放和查看位置。超大画布会自动降低渲染分辨率。

macOS 副屏预览优先用本机缓存的字体、图标和本地素材渲染。整稿仅缺缓存字体时，页面仍显示本地画布并提示字体可能有差异。遇到外链素材或外部组件库时，会按原方式渲染，期间保留上一次画面。连续保存时会跳过已经被新版本取代的预览。

每个任务有固定的本机预览地址，可以收藏或把标签页留在副屏。重启 AI 客户端后，在原任务说一次「开启实时预览」，原标签页会自动重连。从 1.17.0 之前升级时，第一次开启会生成一个新的固定地址。单独改链接资源时，重新开启实时预览可重新渲染。

## 3. 选自己的工作方式

| 使用场景 | 做法 |
| --- | --- |
| 主屏留在 Pen | 打开任务稿，在 AI 会话绑定这份文件并直接描述设计要求。macOS 窗口重开需完成下方的一次性设置。 |
| 主屏工作，副屏看画板 | 说「开启实时预览」，把浏览器页面拖到副屏再手动全屏。后续编辑在后台保存，预览服务不操作 Pen 窗口。 |
| 先看方案再决定 | 请求 AI 先做临时预览。确认后再让 AI 应用到任务稿。 |

同一份稿在浏览器预览期间，使用开启预览的那个 AI 会话修改。如果该稿也在 Pen 中打开，进行后台改稿前先在 Pen 保存并关闭它，以免窗口中的旧版本覆盖新稿。要回到 Pen 窗口工作流，先说「停止实时预览」，再在 Pen 打开最新稿。初次使用建议选择工作副本。多屏可设为扩展显示，并在 macOS「桌面与程序坞 → 调度中心」启用「显示器具有单独的空间」。 [Apple 调度中心设置](https://support.apple.com/zh-cn/guide/mac-help/mchlp1119/mac)

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

其他客户端可添加名为 `local_pen` 的本地 stdio 服务。命令使用 Python 3 的绝对路径，参数是 `-B` 和安装目录中 `local_pen_mcp.py` 的绝对路径。设置 `LOCAL_PEN_NODE` 为 Node 的绝对路径，`LOCAL_PEN_CLI` 为安装目录中 `.runtime/node_modules/.bin/pen` 的绝对路径。保持同一个 AI 会话来使用实时预览。

## 5. 开发验证

在仓库根目录运行以下命令。测试使用临时文件和模拟渲染器，无需连接 AI 客户端或登录 Pen。

```bash
python3 -B -m unittest discover -s tests -p 'test_*.py'
node --test tests/test_pen_font_cache.cjs
bash -n setup_local_pen.sh
```
