# 小克桌面时钟 · Claude-Desktop-Clock

<img src="icon.png" width="128" align="right" alt="icon">

一个免费、高度可配置、极低性能开销的 Windows 桌面时钟~
时钟画在桌面图标后面，像是壁纸自带的一样，不挡窗口、不占任务栏。

当前版本 1.3.3 · [更新记录](CHANGELOG.md)

## 功能

- 时钟在桌面图标后面显示：换壁纸、切换虚拟桌面都会自动跟上
- 任意时区（中文城市名、英文名、UTC+8 都能搜），可选显示秒数、日期和星期
- 位置、字号、颜色、不透明度、文字阴影、时间和日期分别选字体
- 字号单位可选 **磅**（和 Word 一样，跟着系统缩放变）或 **像素**（绝对大小）
- **全屏预览编辑**：截一张当时的桌面，在上面直接拖动时钟、滚轮调大小，方便和桌面上的东西对齐
- Win11 风格的设置窗口，改动实时显示在桌面上；托盘左键打开设置，右键弹出菜单
- 开机自动启动（可关）；设置存在 exe 旁边的 `wallclock.json`，手动改也能自动生效

## 安装

1. 到 [Releases](https://github.com/LW-Arqiu1shz/Claude-Desktop-Clock/releases/latest) 下载 `Claude-Desktop-Clock-版本号.zip` 并解压
2. 把整个文件夹放到一个固定的位置，双击 `小克桌面时钟.exe`（`_internal` 文件夹不能删）
3. 详细用法见 [使用说明.txt](使用说明.txt)

在 Windows 11 上测试过；Windows 10 应该也能用，但还没有实测。

## 从源码运行 / 打包

需要 Python 3 和 `pip install pillow tzdata pyinstaller`。

直接运行：双击 `小克桌面时钟.pyw`。

打包成 exe：

```bash
pyinstaller --noconfirm --onedir --windowed --name ClaudeDesktopClock --icon 小克桌面时钟.ico --add-data "小克桌面时钟.ico;." --collect-data tzdata --exclude-module numpy --exclude-module pygame 小克桌面时钟.pyw
```

打包结果在 `dist\ClaudeDesktopClock\`：把 `ClaudeDesktopClock.exe` 改名成 `小克桌面时钟.exe`，和 `_internal` 放在一起即可。

## 作者

- **Arqiu-owo**：设计
- **Claude**（Anthropic）：代码
- 图标：豆包

## 许可证

- 代码：[LGPL-3.0](LICENSE)
- 图标 `小克桌面时钟.ico` / `icon.png`：CC BY-NC-SA 4.0
