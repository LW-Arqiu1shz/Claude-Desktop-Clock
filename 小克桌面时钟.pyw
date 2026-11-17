"""小克桌面时钟 · Claude Desktop Clock
在桌面壁纸上显示大号时钟（在桌面图标后面，不会盖住任何软件）

设计：Arqiu-owo        代码：Claude        图标：豆包

做法：在桌面图标层的后面放一个窗口，把「原壁纸 + 时间」直接画在这个窗口里。
全程只在内存里画，不生成图片文件，也不换系统壁纸，所以不会写硬盘（开秒数也一样）。
支持多个虚拟桌面：切换桌面时立刻换成那个桌面自己的壁纸。你自己换了壁纸也会自动跟着换。

运行：   双击 小克桌面时钟.exe（或 pythonw 小克桌面时钟.pyw）
关闭：   右键屏幕右下角托盘里的小克图标 →「退出」
         （或 小克桌面时钟.exe --quit / pythonw 小克桌面时钟.pyw --quit）
设置：   托盘菜单 →「设置…」（或 小克桌面时钟.exe --settings）
         设置窗口里的改动通过窗口消息直接发给时钟预览，不写硬盘；点「完成」时才存进 wallclock.json
"""
import ctypes
import glob
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
import winreg
from ctypes import wintypes as W
from datetime import datetime, timezone

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

FROZEN = getattr(sys, "frozen", False)          # 是不是打包好的 exe
# 程序本体所在的文件夹（exe 或 .pyw 旁边），配置文件放在这里
HERE = os.path.dirname(os.path.abspath(sys.executable if FROZEN else sys.argv[0]))
# 打包进 exe 的资源（图标）解压在这里
RES = getattr(sys, "_MEIPASS", HERE)
CONFIG_PATH = os.path.join(HERE, "wallclock.json")
# 旧版本（换壁纸的做法）用过的文件夹；新版本不再往里写图片
OUT_DIR = os.path.join(HERE, "cache")
STATE_PATH = os.path.join(OUT_DIR, "原壁纸记录.json")
APP_NAME = "小克桌面时钟"
APP_NAME_EN = "Claude Desktop Clock"
CREDITS = "设计：Arqiu-owo · 代码：Claude · 图标：豆包"
ABOUT_TEXT = (f"{APP_NAME}  ·  {APP_NAME_EN}\n\n"
              "免费、高度可配置、极低性能开销的桌面时钟~\n\n"
              "设计：Arqiu-owo\n"
              "代码：Claude\n"
              "图标：豆包")
ICON_PATH = os.path.join(RES, "小克桌面时钟.ico")
QUIT_EVENT = r"Local\ClaudeDesktopClock_Quit"
MUTEX_NAME = r"Local\ClaudeDesktopClock_Mutex"
SETTINGS_MUTEX = r"Local\ClaudeDesktopClock_Settings"
SETTINGS_TITLE = f"{APP_NAME} · 设置"
TRAY_CLASS = "ClaudeDesktopClock_Tray"

DEFAULT_CONFIG = {
    "autostart": True,            # 开机自动启动
    "main_tz": "Asia/Shanghai",   # 显示哪个时区的时间
    "show_seconds": False,
    "show_date": True,            # 时间下面的「日期 + 星期」
    "center_x": 0.5,              # 中心位置：占屏幕宽/高的比例，0.5 = 正中
    "center_y": 0.5,
    "size_unit": "pt",            # 字号单位："pt" = 磅（和 Word 一样，跟着屏幕缩放变），"px" = 像素（绝对大小）
    "time_size": 165.0,           # 时间字号（单位看上面）
    "date_size": 24.0,            # 日期字号
    "color": "#ffffff",
    "opacity": 0.9,
    "shadow": True,
    "time_font": "C:/Windows/Fonts/calibri.ttf",
    "text_font": "C:/Windows/Fonts/msyh.ttc",
}

WEEK = "一二三四五六日"

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32.SystemParametersInfoW.argtypes = [W.UINT, W.UINT, ctypes.c_void_p, W.UINT]
kernel32.CreateEventW.restype = W.HANDLE
kernel32.OpenEventW.restype = W.HANDLE
kernel32.CreateMutexW.restype = W.HANDLE
kernel32.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
kernel32.SetEvent.argtypes = [W.HANDLE]
kernel32.ResetEvent.argtypes = [W.HANDLE]
kernel32.GetModuleHandleW.restype = W.HMODULE
SPI_SETDESKWALLPAPER = 0x0014

DESKTOP_KEY = r"Control Panel\Desktop"
VD_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\VirtualDesktops"
HISTORY_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Wallpapers"


def reg_get(path, name):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def current_desktop():
    """当前虚拟桌面的 ID，例如 {6FB60801-...}；没有虚拟桌面信息时返回 None。"""
    raw = reg_get(VD_KEY, "CurrentVirtualDesktop")
    if not raw or len(raw) != 16:
        return None
    return "{" + str(uuid.UUID(bytes_le=bytes(raw))).upper() + "}"


def current_wallpaper():
    """Windows 此刻正在显示的壁纸路径（纯色背景时是空字符串）。

    直接问系统要，不读注册表里「每个虚拟桌面的壁纸」那一项：
    那一项资源管理器不会马上写回，换了壁纸以后它可能还是旧的（之前「壁纸被锁住」就是这个原因）。
    """
    buf = ctypes.create_unicode_buffer(1024)
    if user32.SystemParametersInfoW(0x0073, 1024, buf, 0):   # SPI_GETDESKWALLPAPER
        return buf.value
    return reg_get(DESKTOP_KEY, "WallPaper") or ""


def background_color():
    """「个性化 > 背景 > 纯色」选的颜色。"""
    try:
        r, g, b = (int(v) for v in reg_get(r"Control Panel\Colors", "Background").split())
        return r, g, b
    except Exception:
        return 0, 0, 0


TRANSCODED = os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Themes\TranscodedWallpaper")


def usable_wallpaper(src):
    """真正能读到的壁纸图片路径；纯色背景返回空字符串。

    系统报的路径不一定还在（原图被删了 / 挪走了，或者开着幻灯片放映——Win10 上很常见），
    这时 Windows 显示的其实是它自己存的那份副本 TranscodedWallpaper，就用那份。
    """
    if reg_get(r"Software\Microsoft\Windows\CurrentVersion\Explorer\Wallpapers", "BackgroundType") == 1:
        return ""                                   # 个性化里选的是「纯色」
    if src and os.path.isfile(src):
        return src
    if os.path.isfile(TRANSCODED):
        return TRANSCODED
    return ""


def fit_wallpaper(img, w, h):
    """按「个性化 > 背景 > 选择契合度」把壁纸铺成 w×h，和 Windows 显示的一样。"""
    try:
        style = int(reg_get(DESKTOP_KEY, "WallpaperStyle") or 10)
        tile = int(reg_get(DESKTOP_KEY, "TileWallpaper") or 0)
    except ValueError:
        style, tile = 10, 0
    if style == 2:                                   # 拉伸
        return img.resize((w, h), Image.LANCZOS)
    if style == 6:                                   # 适应：整张图都显示，空出来的地方是背景色
        out = Image.new("RGB", (w, h), background_color())
        im = ImageOps.contain(img, (w, h), Image.LANCZOS)
        out.paste(im, ((w - im.width) // 2, (h - im.height) // 2))
        return out
    if style == 0:
        out = Image.new("RGB", (w, h), background_color())
        if tile:                                     # 平铺
            for y in range(0, h, img.height):
                for x in range(0, w, img.width):
                    out.paste(img, (x, y))
        else:                                        # 居中
            out.paste(img, ((w - img.width) // 2, (h - img.height) // 2))
        return out
    return ImageOps.fit(img, (w, h), Image.LANCZOS)  # 填充（默认）/ 跨区


def load_wallpaper(src, w, h):
    """读壁纸并铺满屏幕；读不到就用纯色背景，并把原因记进日志。"""
    path = usable_wallpaper(src)
    if path:
        try:
            with Image.open(path) as im:
                return fit_wallpaper(im.convert("RGB"), w, h)
        except Exception as e:
            log(f"读不了壁纸 {path!r}：{e!r}")
    elif src:
        log(f"壁纸文件不存在：{src!r}")
    return Image.new("RGB", (w, h), background_color())


def desktop_wallpaper(desk):
    """Windows 注册表里记的某个桌面的壁纸路径（可能过时，只给旧版本清理用）。"""
    if desk:
        v = reg_get(VD_KEY + "\\Desktops\\" + desk, "Wallpaper")
        if v:
            return v
    return reg_get(DESKTOP_KEY, "WallPaper") or ""


def is_ours(path):
    """是不是旧版本生成的图（cache 文件夹里的）。"""
    if not path:
        return False
    a = os.path.normcase(os.path.abspath(path))
    return a.startswith(os.path.normcase(os.path.abspath(OUT_DIR)) + os.sep)


def clean_history():
    """把旧版本生成的图从「设置 > 个性化 > 最近使用的图像」里去掉。"""
    hist = [reg_get(HISTORY_KEY, f"BackgroundHistoryPath{i}") for i in range(5)]
    if not any(is_ours(h) for h in hist):
        return
    keep = [h for h in hist if h and not is_ours(h)]
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, HISTORY_KEY, 0, winreg.KEY_ALL_ACCESS) as k:
            for i in range(5):
                if i < len(keep):
                    winreg.SetValueEx(k, f"BackgroundHistoryPath{i}", 0, winreg.REG_SZ, keep[i])
                else:
                    try:
                        winreg.DeleteValue(k, f"BackgroundHistoryPath{i}")
                    except OSError:
                        pass
    except OSError:
        pass


# ---------------------------------------------------------------- 配置
LOG_PATH = os.path.join(HERE, "错误日志.txt")


def log(msg):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except OSError:
        pass


# 能输入的字号范围
SIZE_RANGE = {"pt": {"time_size": (4, 1000), "date_size": (4, 400)},
              "px": {"time_size": (4, 3000), "date_size": (4, 1200)}}


def round_size(v, unit):
    """磅和 Word 一样精确到 0.5，像素取整数。"""
    return round(v * 2) / 2 if unit == "pt" else float(round(v))


def clamp_size(cfg, key, v):
    lo, hi = SIZE_RANGE[cfg["size_unit"]][key]
    return min(hi, max(lo, round_size(v, cfg["size_unit"])))


def size_px(cfg, key):
    """字号换算成屏幕上的像素。"""
    v = cfg[key]
    return max(4, round(v * px_per_pt() if cfg["size_unit"] == "pt" else v))


def px_per_pt():
    """1 磅 = 1/72 英寸；屏幕每英寸多少像素由「缩放」决定（100% = 96，150% = 144）。和 Word 的算法一样。"""
    try:
        dpi = user32.GetDpiForSystem()
    except Exception:
        dpi = 96
    return (dpi or 96) / 72


def normalize(user):
    """把一份设置补全成完整、类型正确的配置；参数不对就直接报错。"""
    if not isinstance(user, dict):
        raise ValueError("最外层必须是 { ... }")
    user = dict(user)
    if "size_unit" not in user:
        if "time_pt" in user or "date_pt" in user:
            # 上一版：time_pt / date_pt，单位是磅
            for old, new in (("time_pt", "time_size"), ("date_pt", "date_size")):
                if old in user:
                    user[new] = user[old]
        else:
            # 更早的版本：字号是「按 1600 像素高的屏幕算的像素」，按这台电脑的屏幕换算成磅，看起来一样大
            for k in ("time_size", "date_size"):
                if k in user:
                    px = float(user[k]) * user32.GetSystemMetrics(1) / 1600
                    user[k] = round(px / px_per_pt() * 2) / 2
        user["size_unit"] = "pt"
    cfg = dict(DEFAULT_CONFIG)
    cfg.update({k: v for k, v in user.items() if k in DEFAULT_CONFIG})
    color = str(cfg["color"]).strip()
    if not color.startswith("#") or len(color) < 7:
        raise ValueError(f"颜色要写成 #rrggbb：{color}")
    int(color[1:7], 16)
    cfg["color"] = color[:7]
    for k in ("center_x", "center_y", "opacity"):
        cfg[k] = float(cfg[k])
    cfg["size_unit"] = str(cfg["size_unit"]).strip().lower()
    if cfg["size_unit"] not in SIZE_RANGE:
        raise ValueError(f"size_unit 只能是 \"pt\"（磅）或者 \"px\"（像素）：{cfg['size_unit']}")
    for k in ("time_size", "date_size"):
        cfg[k] = clamp_size(cfg, k, float(cfg[k]))
    for k in ("autostart", "show_seconds", "show_date", "shadow"):
        cfg[k] = bool(cfg[k])
    for k in ("main_tz", "time_font", "text_font"):
        cfg[k] = str(cfg[k])
    return cfg


def save_config(cfg):
    """写 wallclock.json：先写临时文件再替换，时钟不会读到写了一半的文件。"""
    tmp = CONFIG_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except OSError as e:
        log(f"保存设置失败：{e}")


def load_config(previous=None):
    """读取 wallclock.json。文件写错时保留上一次能用的配置，并把原因写进 错误日志.txt。"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            return normalize(json.load(f))
    except FileNotFoundError:
        cfg = dict(DEFAULT_CONFIG)
        save_config(cfg)
        return cfg
    except Exception as e:
        log(f"wallclock.json 有问题，继续用上一次的设置：{e}")
        return previous or dict(DEFAULT_CONFIG)


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = APP_NAME
OLD_RUN_NAMES = ("壁纸时钟",)


def autostart_command():
    if FROZEN:
        return f'"{sys.executable}"'
    # 优先用 WindowsApps 里的 pythonw 入口：商店版 Python 升级后路径也不会变
    alias = os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Microsoft\WindowsApps\pythonw.exe")
    exe = alias if os.path.exists(alias) else sys.executable.replace("python.exe", "pythonw.exe")
    return f'"{exe}" "{os.path.abspath(sys.argv[0])}"'


def sync_autostart(enabled):
    """按配置在「当前用户 - 开机启动项」里添加或删除小克桌面时钟。"""
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            for old in OLD_RUN_NAMES:          # 改名之前的启动项
                try:
                    winreg.DeleteValue(k, old)
                except OSError:
                    pass
            try:
                current = winreg.QueryValueEx(k, RUN_NAME)[0]
            except FileNotFoundError:
                current = None
            if enabled:
                cmd = autostart_command()
                if current != cmd:
                    winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, cmd)
            elif current is not None:
                winreg.DeleteValue(k, RUN_NAME)
    except OSError as e:
        log(f"设置开机自启失败：{e}")


def sync_shortcuts():
    """在程序文件夹里放「启动 / 关闭」两个快捷方式（带图标）。

    快捷方式里只能写死完整路径，所以文件夹被移动或改名后它们就失效了。
    每次启动都检查一下，路径变了就重新生成。
    打包成 exe 时不需要：双击 exe 就是启动，托盘菜单里就能退出。
    """
    if FROZEN:
        return
    script = os.path.abspath(sys.argv[0])
    stamp = os.path.join(OUT_DIR, "快捷方式路径.txt")
    want = {os.path.join(HERE, f"启动{APP_NAME}.lnk"): "",
            os.path.join(HERE, f"关闭{APP_NAME}.lnk"): "--quit"}
    try:
        with open(stamp, encoding="utf-8") as f:
            if f.read() == script and all(os.path.exists(p) for p in want):
                return
    except OSError:
        pass
    for old in ("启动壁纸时钟.lnk", "关闭壁纸时钟.lnk"):
        try:
            os.remove(os.path.join(HERE, old))
        except OSError:
            pass
    exe = autostart_command().split('" "')[0].strip('"')
    q = lambda v: "'" + v.replace("'", "''") + "'"
    ps = ["$sh = New-Object -ComObject WScript.Shell"]
    for lnk, arg in want.items():
        ps += [f"$l = $sh.CreateShortcut({q(lnk)})",
               f"$l.TargetPath = {q(exe)}",
               f"$l.Arguments = {q(chr(34) + script + chr(34) + (' ' + arg if arg else ''))}",
               f"$l.WorkingDirectory = {q(HERE)}",
               f"$l.IconLocation = {q(ICON_PATH + ',0')}",
               f"$l.Description = {q(APP_NAME + ' / ' + APP_NAME_EN + ' — ' + CREDITS)}",
               "$l.Save()"]
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", "; ".join(ps)],
                       creationflags=0x08000000, timeout=30, check=True)   # 不弹黑窗
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(stamp, "w", encoding="utf-8") as f:
            f.write(script)
    except Exception as e:
        log(f"生成快捷方式失败：{e!r}")


def tz_of(name):
    try:
        return ZoneInfo(name)
    except Exception:
        return None


def font(path, size):
    for p in (path, "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/segoeui.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


# ---------------------------------------------------------------- 绘制
def style_key(cfg):
    """影响文字长相的那些设置（不含位置）。位置变了只需要挪一下，不用重画。"""
    return json.dumps({k: v for k, v in cfg.items() if k not in ("center_x", "center_y", "autostart")},
                      sort_keys=True)


def place(cfg, w, h, offset):
    """文字块左上角在屏幕上的位置。"""
    return int(w * cfg["center_x"]) + offset[0], int(h * cfg["center_y"]) + offset[1]


def clock_now(cfg):
    tz = tz_of(cfg["main_tz"]) or datetime.now().astimezone().tzinfo
    return datetime.now(timezone.utc).astimezone(tz)


def time_label(cfg, now):
    return now.strftime("%Y%m%d %H:%M:%S" if cfg["show_seconds"] else "%Y%m%d %H:%M")


def render(cfg, w, h, now):
    """只画文字所在的那一小块（带阴影）。

    返回 (RGBA 小图, 偏移)：偏移是小图左上角相对于时钟中心点的位置，用 place() 换算成屏幕坐标。
    """
    # 磅和 Word 一样：同样的字号，在桌面上和在 Word 里（100% 显示比例）一样大；像素就是像素
    tsize = size_px(cfg, "time_size")
    dsize = size_px(cfg, "date_size")
    time_text = now.strftime("%H:%M:%S" if cfg["show_seconds"] else "%H:%M")
    lines = [(time_text, font(cfg["time_font"], tsize), 0)]
    if cfg["show_date"]:
        date_text = f"{now.year}年{now.month}月{now.day}日   星期{WEEK[now.weekday()]}"
        lines.append((date_text, font(cfg["text_font"], dsize), int(dsize * 0.5)))

    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    boxes = [probe.textbbox((0, 0), t, font=f) for t, f, _ in lines]
    total = sum(b[3] - b[1] for b in boxes) + sum(g for _, _, g in lines)
    width = max(b[2] - b[0] for b in boxes)

    # 整个文字块（时间 + 日期）水平、垂直居中于时钟中心点
    blur = max(6, tsize // 30)
    pad = blur * 3 + 8                      # 给阴影留的边
    mid = width // 2 + pad                  # 中心点在小图里的横坐标
    cw, ch = width + 2 * pad, total + 2 * pad

    mask = Image.new("L", (cw, ch), 0)
    d = ImageDraw.Draw(mask)
    y = pad
    for (t, f, gap), b in zip(lines, boxes):
        y += gap
        d.text((mid - (b[0] + b[2]) // 2, y - b[1]), t, font=f, fill=255)
        y += b[3] - b[1]

    r, g, bl = (int(cfg["color"][i:i + 2], 16) for i in (1, 3, 5))
    opacity = float(cfg["opacity"])
    alpha = mask.point(lambda v: int(v * opacity))
    img = Image.merge("RGBA", (Image.new("L", (cw, ch), r), Image.new("L", (cw, ch), g),
                               Image.new("L", (cw, ch), bl), alpha))
    if cfg["shadow"]:
        soft = mask.filter(ImageFilter.GaussianBlur(blur))
        tight = mask.filter(ImageFilter.GaussianBlur(3))
        sh = ImageChops.lighter(soft.point(lambda v: int(v * 0.55)),
                                tight.point(lambda v: int(v * 0.6)))
        # 阴影只留在文字外面：文字底下挖空，半透明的文字透出来的是壁纸，不是黑影；
        # 阴影也跟着不透明度一起变淡
        sh = ImageChops.multiply(sh, ImageChops.invert(mask)).point(lambda v: int(v * opacity))
        shadow = Image.merge("RGBA", (Image.new("L", (cw, ch), 0),) * 3 + (sh,))
        shadow.alpha_composite(img)
        img = shadow
    return img, (-mid, -(total // 2) - pad)


# ---------------------------------------------------------------- 桌面图标后面的窗口
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, W.HWND, W.UINT, W.WPARAM, W.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", W.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", W.HINSTANCE), ("hIcon", W.HICON),
                ("hCursor", W.HANDLE), ("hbrBackground", W.HBRUSH),
                ("lpszMenuName", W.LPCWSTR), ("lpszClassName", W.LPCWSTR)]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [("hdc", W.HDC), ("fErase", W.BOOL), ("rcPaint", W.RECT), ("fRestore", W.BOOL),
                ("fIncUpdate", W.BOOL), ("rgbReserved", ctypes.c_byte * 32)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", W.DWORD), ("biWidth", W.LONG), ("biHeight", W.LONG),
                ("biPlanes", W.WORD), ("biBitCount", W.WORD), ("biCompression", W.DWORD),
                ("biSizeImage", W.DWORD), ("biXPelsPerMeter", W.LONG),
                ("biYPelsPerMeter", W.LONG), ("biClrUsed", W.DWORD), ("biClrImportant", W.DWORD)]


user32.DefWindowProcW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.CreateWindowExW.argtypes = [W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, W.HWND, W.HMENU,
                                   W.HINSTANCE, W.LPVOID]
user32.CreateWindowExW.restype = W.HWND
user32.FindWindowW.restype = W.HWND
user32.FindWindowExW.argtypes = [W.HWND, W.HWND, W.LPCWSTR, W.LPCWSTR]
user32.FindWindowExW.restype = W.HWND
user32.GetParent.argtypes = [W.HWND]
user32.GetParent.restype = W.HWND
user32.IsWindow.argtypes = [W.HWND]
user32.DestroyWindow.argtypes = [W.HWND]
user32.SetWindowPos.argtypes = [W.HWND, W.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, W.UINT]
user32.InvalidateRect.argtypes = [W.HWND, ctypes.POINTER(W.RECT), W.BOOL]
user32.BeginPaint.argtypes = [W.HWND, ctypes.POINTER(PAINTSTRUCT)]
user32.BeginPaint.restype = W.HDC
user32.EndPaint.argtypes = [W.HWND, ctypes.POINTER(PAINTSTRUCT)]
user32.SetLayeredWindowAttributes.argtypes = [W.HWND, W.COLORREF, W.BYTE, W.DWORD]
user32.SendMessageTimeoutW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM, W.UINT, W.UINT,
                                       ctypes.POINTER(ctypes.c_size_t)]
user32.MsgWaitForMultipleObjects.argtypes = [W.DWORD, ctypes.POINTER(W.HANDLE), W.BOOL,
                                             W.DWORD, W.DWORD]
user32.PeekMessageW.argtypes = [ctypes.POINTER(W.MSG), W.HWND, W.UINT, W.UINT, W.UINT]
gdi32.SetDIBitsToDevice.argtypes = [W.HDC, ctypes.c_int, ctypes.c_int, W.DWORD, W.DWORD,
                                    ctypes.c_int, ctypes.c_int, W.UINT, W.UINT, ctypes.c_void_p,
                                    ctypes.c_void_p, W.UINT]


def find_wallpaper_host():
    """找到 Windows 用来画壁纸的 WorkerW 窗口（它在桌面图标层的后面）。"""
    progman = user32.FindWindowW("Progman", None)
    if not progman:
        return None
    # 0x052C：让资源管理器把壁纸层单独拆成一个 WorkerW（Wallpaper Engine 等软件也是这么做的）
    res = ctypes.c_size_t()
    user32.SendMessageTimeoutW(progman, 0x052C, 0xD, 0x1, 0, 1000, ctypes.byref(res))
    # Win11 24H2 以后：WorkerW 是 Progman 的子窗口
    ww = user32.FindWindowExW(progman, None, "WorkerW", None)
    if ww:
        return ww
    # 更早的 Windows：WorkerW 是紧跟在含有图标层的那个顶层窗口后面的兄弟窗口
    found = []

    @ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    def each(h, _):
        if user32.FindWindowExW(h, None, "SHELLDLL_DefView", None):
            nxt = user32.FindWindowExW(None, h, "WorkerW", None)
            if nxt:
                found.append(nxt)
        return True

    user32.EnumWindows(each, 0)
    return found[0] if found else None


class Canvas:
    """铺满屏幕、贴在壁纸层上的窗口。所有画面都在内存里，不写文件。"""

    def __init__(self):
        self.hwnd = None
        self.host = None
        self.frame = None            # 当前整屏画面（PIL RGB）
        self.size = (0, 0)
        self._proc = WNDPROC(self._wndproc)   # 必须一直留着引用，否则会被回收
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._proc
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.lpszClassName = "ClaudeDesktopClock_by_Claude_and_Arqiu-owo"
        wc.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(32512))
        user32.RegisterClassW(ctypes.byref(wc))
        self.hinst = wc.hInstance
        self.wc_name = wc.lpszClassName

    def alive(self):
        return bool(self.hwnd and user32.IsWindow(self.hwnd)
                    and self.host and user32.IsWindow(self.host)
                    and user32.GetParent(self.hwnd) == self.host)

    def ensure(self, w, h):
        """窗口没了（第一次 / 资源管理器重启过）就重新建；返回 True 表示需要整屏重画。"""
        if self.alive():
            if self.size != (w, h):
                user32.SetWindowPos(self.hwnd, None, 0, 0, w, h, 0x0014)  # NOZORDER|NOACTIVATE
                self.size = (w, h)
                return True
            return False
        if self.hwnd and user32.IsWindow(self.hwnd):
            user32.DestroyWindow(self.hwnd)
        self.hwnd = None
        self.host = find_wallpaper_host()
        if not self.host:
            return False
        # WS_CHILD | WS_VISIBLE | WS_DISABLED（不接收鼠标），WS_EX_LAYERED：24H2 的壁纸层只显示分层子窗口
        self.hwnd = user32.CreateWindowExW(0x00080000 | 0x08000000 | 0x20, self.wc_name, f"{APP_NAME} · {APP_NAME_EN} ({CREDITS})",
                                           0x40000000 | 0x10000000 | 0x08000000,
                                           0, 0, w, h, self.host, None, self.hinst, None)
        if not self.hwnd:
            log(f"创建窗口失败：{ctypes.get_last_error()}")
            return False
        user32.SetLayeredWindowAttributes(self.hwnd, 0, 255, 2)   # LWA_ALPHA，完全不透明
        self.size = (w, h)
        return True

    def invalidate(self, box=None):
        if not self.hwnd:
            return
        if box is None:
            user32.InvalidateRect(self.hwnd, None, False)
        else:
            r = W.RECT(*box)
            user32.InvalidateRect(self.hwnd, ctypes.byref(r), False)

    def _wndproc(self, hwnd, msg, wp, lp):
        if msg == 0x000F:   # WM_PAINT
            ps = PAINTSTRUCT()
            hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
            try:
                self._paint(hdc, ps.rcPaint)
            except Exception as e:
                log(f"绘制失败：{e!r}")
            user32.EndPaint(hwnd, ctypes.byref(ps))
            return 0
        if msg == 0x0014:   # WM_ERASEBKGND：整屏都由我们自己画，不用擦，避免闪
            return 1
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _paint(self, hdc, rc):
        if self.frame is None:
            return
        fw, fh = self.frame.size
        l, t = max(0, rc.left), max(0, rc.top)
        r, b = min(fw, rc.right), min(fh, rc.bottom)
        if r <= l or b <= t:
            return
        data = self.frame.crop((l, t, r, b)).tobytes("raw", "BGRX")
        bi = BITMAPINFOHEADER()
        bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.biWidth, bi.biHeight = r - l, -(b - t)   # 负数 = 从上往下
        bi.biPlanes, bi.biBitCount = 1, 32
        gdi32.SetDIBitsToDevice(hdc, l, t, r - l, b - t, 0, 0, 0, b - t,
                                data, ctypes.byref(bi), 0)

    def destroy(self):
        if self.hwnd and user32.IsWindow(self.hwnd):
            user32.DestroyWindow(self.hwnd)
        self.hwnd = None


# ---------------------------------------------------------------- 托盘图标（右下角）
class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", W.DWORD), ("hWnd", W.HWND), ("uID", W.UINT), ("uFlags", W.UINT),
                ("uCallbackMessage", W.UINT), ("hIcon", W.HICON), ("szTip", W.WCHAR * 128),
                ("dwState", W.DWORD), ("dwStateMask", W.DWORD), ("szInfo", W.WCHAR * 256),
                ("uVersion", W.UINT), ("szInfoTitle", W.WCHAR * 64), ("dwInfoFlags", W.DWORD),
                ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", W.HICON)]


shell32 = ctypes.WinDLL("shell32", use_last_error=True)
shell32.Shell_NotifyIconW.argtypes = [W.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
user32.LoadImageW.argtypes = [W.HINSTANCE, W.LPCWSTR, W.UINT, ctypes.c_int, ctypes.c_int, W.UINT]
user32.LoadImageW.restype = W.HANDLE
user32.SetForegroundWindow.argtypes = [W.HWND]
user32.PostMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
user32.MessageBoxW.argtypes = [W.HWND, W.LPCWSTR, W.LPCWSTR, W.UINT]
user32.FindWindowW.argtypes = [W.LPCWSTR, W.LPCWSTR]
user32.ShowWindow.argtypes = [W.HWND, ctypes.c_int]
user32.GetDpiForWindow.argtypes = [W.HWND]
user32.GetDpiForWindow.restype = W.UINT
user32.MonitorFromPoint.argtypes = [W.POINT, W.DWORD]
user32.MonitorFromPoint.restype = W.HMONITOR
user32.GetMonitorInfoW.argtypes = [W.HMONITOR, ctypes.c_void_p]
user32.SetWindowPos.argtypes = [W.HWND, W.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, W.UINT]
user32.DestroyWindow.argtypes = [W.HWND]
user32.TrackMouseEvent.argtypes = [ctypes.c_void_p]
user32.SetTimer.argtypes = [W.HWND, ctypes.c_size_t, W.UINT, ctypes.c_void_p]
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short

WM_TRAY = 0x8001
WM_COPYDATA = 0x004A
PREVIEW_TAG = 0xC10C                  # 设置窗口发来的预览
MENU_AUTOSTART, MENU_SECONDS, MENU_DATE, MENU_SETTINGS, MENU_ABOUT, MENU_QUIT = range(1, 7)


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", W.DWORD), ("rcMonitor", W.RECT), ("rcWork", W.RECT), ("dwFlags", W.DWORD)]


class TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [("cbSize", W.DWORD), ("dwFlags", W.DWORD), ("hwndTrack", W.HWND), ("dwHoverTime", W.DWORD)]


def light_theme():
    """系统（任务栏、菜单）用的是不是浅色。"""
    v = reg_get(r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize", "SystemUsesLightTheme")
    return v != 0


class PopupMenu:
    """托盘右键菜单。

    Windows 自带的菜单上下边距是定死的（第一项、最后一项会贴边），只要掺一点自己画的东西
    就会整个退回老式菜单，所以这里整个自己画：Win11 风格的圆角、阴影、深浅色，所有间距都一致。
    items：("title", 文字) / ("sep",) / ("item", 命令, 文字, 打勾, 加粗)
    """
    CLASS = "ClaudeDesktopClock_Menu"
    # 100% 缩放下的像素；四周留白 = 高亮块到分隔线的距离 = 6
    EDGE, INSET, ROW, SEP, CHECK_W, RIGHT, RADIUS, MIN_W = 4, 2, 28, 9, 32, 28, 4, 160

    def __init__(self):
        self.hwnd = None
        self._proc = WNDPROC(self._wndproc)
        wc = WNDCLASSW()
        wc.style = 0x00020000                      # CS_DROPSHADOW
        wc.lpfnWndProc = self._proc
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.lpszClassName = self.CLASS
        wc.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(32512))
        user32.RegisterClassW(ctypes.byref(wc))
        self.hinst = wc.hInstance

    def show(self, items, on_pick):
        self.close()
        self.items, self.on_pick, self.hover = items, on_pick, None
        pt = W.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        mon = user32.MonitorFromPoint(pt, 2)       # MONITOR_DEFAULTTONEAREST
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(mi)
        user32.GetMonitorInfoW(mon, ctypes.byref(mi))
        dx, dy = W.UINT(), W.UINT()
        try:
            ctypes.windll.shcore.GetDpiForMonitor(mon, 0, ctypes.byref(dx), ctypes.byref(dy))
        except Exception:
            dx.value = 96
        self.s = s = (dx.value or 96) / 96
        self.light = light_theme()
        px = lambda v: max(1, round(v * s))
        self.font = font_index("C:/Windows/Fonts/msyh.ttc", 1, px(12))
        self.bold = font_index("C:/Windows/Fonts/msyhbd.ttc", 1, px(12))
        icons = "C:/Windows/Fonts/SegoeIcons.ttf"
        self.glyph = font_index(icons if os.path.exists(icons) else "C:/Windows/Fonts/segmdl2.ttf", 0, px(12))

        # 每一行的位置
        self.rows, y = [], px(self.EDGE)
        width = px(self.MIN_W)
        for it in items:
            h = px(self.SEP) if it[0] == "sep" else px(self.ROW)
            self.rows.append((y, y + h))
            y += h
            if it[0] != "sep":
                f = self.bold if it[0] == "item" and it[4] else self.font
                text = it[1] if it[0] == "title" else it[2]
                width = max(width, px(self.CHECK_W) + int(f.getlength(text)) + px(self.RIGHT))
        self.w, self.h = width + 2 * px(self.EDGE), y + px(self.EDGE)

        wk = mi.rcWork
        x = pt.x if pt.x + self.w <= wk.right else pt.x - self.w
        y = pt.y if pt.y + self.h <= wk.bottom else pt.y - self.h
        x, y = max(wk.left, x), max(wk.top, y)
        self.hwnd = user32.CreateWindowExW(0x80 | 0x8, self.CLASS, APP_NAME, 0x80000000,  # TOOLWINDOW|TOPMOST, POPUP
                                           x, y, self.w, self.h, None, None, self.hinst, None)
        if not self.hwnd:
            return
        self.rounded = False
        try:   # Win11：系统画圆角和细边框（Win10 没有这个设置，就自己画一圈边框）
            pref = ctypes.c_int(3)                 # DWMWCP_ROUNDSMALL，和系统菜单一样
            self.rounded = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                W.HWND(self.hwnd), 33, ctypes.byref(pref), 4) == 0
        except Exception:
            pass
        self.render()
        user32.ShowWindow(self.hwnd, 5)            # SW_SHOW
        user32.SetForegroundWindow(self.hwnd)
        # 保险：万一没拿到前台（就收不到「点了外面」的通知），也定时看看是不是在菜单外面按了鼠标
        self.rect = (x, y, x + self.w, y + self.h)
        user32.SetTimer(self.hwnd, 1, 50, None)

    def render(self):
        s, px = self.s, lambda v: max(1, round(v * self.s))
        if self.light:
            bg, fg, gray, hov, line = (249, 249, 249), (26, 26, 26), (140, 140, 140), (234, 234, 234), (229, 229, 229)
        else:
            bg, fg, gray, hov, line = (44, 44, 44), (255, 255, 255), (150, 150, 150), (61, 61, 61), (64, 64, 64)
        img = Image.new("RGB", (self.w, self.h), bg)
        d = ImageDraw.Draw(img)
        left, right = px(self.EDGE), self.w - px(self.EDGE)
        for i, (it, (y0, y1)) in enumerate(zip(self.items, self.rows)):
            if it[0] == "sep":
                mid = (y0 + y1) // 2
                d.rectangle((left + px(self.INSET), mid, right - px(self.INSET) - 1, mid + px(1) - 1), fill=line)
                continue
            if i == self.hover and it[0] == "item":
                d.rounded_rectangle((left + px(self.INSET), y0 + px(self.INSET),
                                     right - px(self.INSET) - 1, y1 - px(self.INSET) - 1),
                                    px(self.RADIUS), fill=hov)
            cy = (y0 + y1) / 2
            if it[0] == "title":
                text, f, color = it[1], self.font, gray
            else:
                text, f, color = it[2], (self.bold if it[4] else self.font), fg
                if it[3]:
                    d.text((left + px(self.CHECK_W) / 2, cy), "\uE73E", font=self.glyph, fill=fg, anchor="mm")
            d.text((left + px(self.CHECK_W), cy), text, font=f, fill=color, anchor="lm")
        if not self.rounded:
            d.rectangle((0, 0, self.w - 1, self.h - 1), outline=(200, 200, 200) if self.light else (80, 80, 80))
        self.frame = img
        if self.hwnd:
            user32.InvalidateRect(self.hwnd, None, False)

    def hit(self, y):
        for i, (y0, y1) in enumerate(self.rows):
            if y0 <= y < y1 and self.items[i][0] == "item":
                return i
        return None

    def set_hover(self, i):
        if i != self.hover:
            self.hover = i
            self.render()

    def pick(self, i):
        if i is None:
            return
        cmd = self.items[i][1]
        self.close()
        self.on_pick(cmd)

    def close(self):
        if self.hwnd:
            h, self.hwnd = self.hwnd, None
            user32.DestroyWindow(h)

    def _wndproc(self, hwnd, msg, wp, lp):
        try:
            if msg == 0x000F:                          # WM_PAINT
                ps = PAINTSTRUCT()
                hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
                bi = BITMAPINFOHEADER()
                bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
                bi.biWidth, bi.biHeight = self.w, -self.h
                bi.biPlanes, bi.biBitCount = 1, 32
                gdi32.SetDIBitsToDevice(hdc, 0, 0, self.w, self.h, 0, 0, 0, self.h,
                                        self.frame.tobytes("raw", "BGRX"), ctypes.byref(bi), 0)
                user32.EndPaint(hwnd, ctypes.byref(ps))
                return 0
            if msg == 0x0014:                          # WM_ERASEBKGND
                return 1
            if hwnd != self.hwnd:
                return user32.DefWindowProcW(hwnd, msg, wp, lp)
            if msg == 0x0200:                          # WM_MOUSEMOVE
                tme = TRACKMOUSEEVENT(ctypes.sizeof(TRACKMOUSEEVENT), 0x2, hwnd, 0)   # TME_LEAVE
                user32.TrackMouseEvent(ctypes.byref(tme))
                self.set_hover(self.hit(ctypes.c_short(lp >> 16).value))
                return 0
            if msg == 0x02A3:                          # WM_MOUSELEAVE
                self.set_hover(None)
                return 0
            if msg in (0x0202, 0x0205):                # 左键 / 右键松开
                self.pick(self.hit(ctypes.c_short(lp >> 16).value))
                return 0
            if msg == 0x0100:                          # WM_KEYDOWN
                live = [i for i, it in enumerate(self.items) if it[0] == "item"]
                if wp == 0x1B:                         # Esc
                    self.close()
                elif wp in (0x26, 0x28):               # ↑ ↓
                    pos = live.index(self.hover) if self.hover in live else -1
                    pos = (pos + (1 if wp == 0x28 else -1)) % len(live) if pos >= 0 else \
                        (0 if wp == 0x28 else len(live) - 1)
                    self.set_hover(live[pos])
                elif wp == 0x0D:                       # Enter
                    self.pick(self.hover)
                return 0
            if msg == 0x0113:                          # WM_TIMER
                pressed = any(user32.GetAsyncKeyState(k) & 0x8000 for k in (0x01, 0x02, 0x04))
                if pressed:
                    pt = W.POINT()
                    user32.GetCursorPos(ctypes.byref(pt))
                    l, t, r, b = self.rect
                    if not (l <= pt.x < r and t <= pt.y < b):
                        self.close()
                return 0
            if msg == 0x0006 and (wp & 0xFFFF) == 0:   # WM_ACTIVATE：点了菜单外面
                user32.PostMessageW(hwnd, 0x0010, 0, 0)
                return 0
            if msg == 0x0010:                          # WM_CLOSE
                if hwnd == self.hwnd:
                    self.close()
                return 0
        except Exception as e:
            log(f"菜单出错：{e!r}")
        return user32.DefWindowProcW(hwnd, msg, wp, lp)


def font_index(path, index, size):
    try:
        return ImageFont.truetype(path, size, index=index)
    except Exception:
        return font(path, size)


class COPYDATASTRUCT(ctypes.Structure):
    _fields_ = [("dwData", ctypes.c_size_t), ("cbData", W.DWORD), ("lpData", ctypes.c_void_p)]


def send_preview(cfg):
    """把设置直接发给正在运行的时钟（窗口消息，不经过硬盘）。返回时钟在不在。"""
    hwnd = user32.FindWindowW(TRAY_CLASS, None)
    if not hwnd:
        return False
    data = json.dumps(cfg, ensure_ascii=False).encode("utf-8")
    buf = ctypes.create_string_buffer(data, len(data))
    cds = COPYDATASTRUCT(PREVIEW_TAG, len(data), ctypes.cast(buf, ctypes.c_void_p))
    res = ctypes.c_size_t()
    user32.SendMessageTimeoutW(hwnd, WM_COPYDATA, 0, ctypes.addressof(cds),
                               0x0002, 1000, ctypes.byref(res))   # SMTO_ABORTIFHUNG
    return True


def clock_running():
    return bool(user32.FindWindowW(TRAY_CLASS, None))


def launch(*args):
    """再开一个自己（exe 或 .pyw），比如打开设置窗口、启动时钟。"""
    cmd = [sys.executable, *args] if FROZEN else [sys.executable, os.path.abspath(sys.argv[0]), *args]
    subprocess.Popen(cmd, cwd=HERE)


def set_option(key, value):
    """改 wallclock.json 里的一项（保留文件里其它内容），时钟会自动读到新设置。"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError
    except Exception:
        data = dict(DEFAULT_CONFIG)
    data[key] = value
    save_config(data)


class Tray:
    def __init__(self, clock, on_quit):
        self.clock = clock
        self.on_quit = on_quit
        self.hwnd = None
        self.popup = PopupMenu()
        # 要在建窗口之前准备好：建窗口的过程中就会收到消息
        self.taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
        self._proc = WNDPROC(self._wndproc)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._proc
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.lpszClassName = TRAY_CLASS
        user32.RegisterClassW(ctypes.byref(wc))
        # 看不见的普通窗口：用来接收托盘图标的点击（以及资源管理器重启的通知）
        self.hwnd = user32.CreateWindowExW(0, wc.lpszClassName, APP_NAME, 0x80000000,
                                           0, 0, 0, 0, None, None, wc.hInstance, None)
        n = user32.GetSystemMetrics(49)   # 托盘小图标的尺寸
        self.icon = user32.LoadImageW(None, ICON_PATH, 1, n, n, 0x10) or \
            user32.LoadIconW(None, ctypes.c_void_p(32512))
        self.add()

    def _data(self):
        d = NOTIFYICONDATAW()
        d.cbSize = ctypes.sizeof(d)
        d.hWnd = self.hwnd
        d.uID = 1
        d.uFlags = 0x1 | 0x2 | 0x4           # NIF_MESSAGE | NIF_ICON | NIF_TIP
        d.uCallbackMessage = WM_TRAY
        d.hIcon = self.icon
        d.szTip = f"{APP_NAME} · {APP_NAME_EN}"
        return d

    def add(self):
        shell32.Shell_NotifyIconW(0, ctypes.byref(self._data()))   # NIM_ADD

    def remove(self):
        self.popup.close()
        shell32.Shell_NotifyIconW(2, ctypes.byref(self._data()))   # NIM_DELETE
        if self.hwnd:
            user32.DestroyWindow(self.hwnd)
            self.hwnd = None

    def _wndproc(self, hwnd, msg, wp, lp):
        try:
            if msg == WM_TRAY and lp == 0x0202:             # 左键松开：打开设置窗口
                launch("--settings")
                return 0
            if msg == WM_TRAY and lp == 0x0205:             # 右键松开：弹出小菜单
                self.menu()
                return 0
            if msg == self.taskbar_created:                 # 资源管理器重启了，重新放图标
                self.add()
                return 0
            if msg == WM_COPYDATA and lp:
                cds = ctypes.cast(lp, ctypes.POINTER(COPYDATASTRUCT)).contents
                if cds.dwData == PREVIEW_TAG and cds.lpData:
                    raw = ctypes.string_at(cds.lpData, cds.cbData)
                    self.clock.preview(json.loads(raw.decode("utf-8")))
                    return 1
        except Exception as e:
            log(f"托盘出错：{e!r}")
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def menu(self):
        cfg = self.clock.cfg
        self.popup.show([
            ("title", APP_NAME),
            ("sep",),
            ("item", MENU_SETTINGS, "设置…", False, True),
            ("sep",),
            ("item", MENU_AUTOSTART, "开机自动启动", cfg["autostart"], False),
            ("item", MENU_SECONDS, "显示秒数", cfg["show_seconds"], False),
            ("item", MENU_DATE, "显示日期和星期", cfg["show_date"], False),
            ("sep",),
            ("item", MENU_ABOUT, "关于", False, False),
            ("item", MENU_QUIT, "退出", False, False),
        ], lambda cmd: self.command(cmd, cfg))

    def command(self, cmd, cfg):
        if cmd == MENU_AUTOSTART:
            set_option("autostart", not cfg["autostart"])
        elif cmd == MENU_SECONDS:
            set_option("show_seconds", not cfg["show_seconds"])
        elif cmd == MENU_DATE:
            set_option("show_date", not cfg["show_date"])
        elif cmd == MENU_SETTINGS:
            launch("--settings")
        elif cmd == MENU_ABOUT:
            # 放到别的线程弹，免得弹窗开着的时候时钟停走
            threading.Thread(target=user32.MessageBoxW,
                             args=(None, ABOUT_TEXT, f"关于 {APP_NAME}", 0x40 | 0x10000),
                             daemon=True).start()
        elif cmd == MENU_QUIT:
            self.on_quit()


# ---------------------------------------------------------------- 主循环
class WallClock:
    def __init__(self):
        self.cfg = load_config()
        self.cfg_mtime = self.mtime()
        sync_autostart(self.cfg["autostart"])
        self.originals = self.load_state()   # 桌面ID -> 原壁纸路径（旧版本留下的记录）
        self.bases = {}                       # (路径, 修改时间, 宽, 高) -> 缩放好的原壁纸
        self.canvas = Canvas()
        self.text_key = None
        self.text_img = None                  # (RGBA 小图, 相对中心点的偏移)
        self.layer = None                     # (RGBA 小图, 左上角)
        self.base_key = None
        self.drawn_box = None                 # 上一次文字在屏幕上占的位置

    # ---- 旧版本留下的东西
    def load_state(self):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def cleanup_old_version(self):
        """旧版本是直接换壁纸的：把原壁纸换回来，删掉它生成的图片。"""
        old = glob.glob(os.path.join(OUT_DIR, "wallclock_*.*"))
        if old or is_ours(desktop_wallpaper(current_desktop())):
            src = self.original_of(current_desktop())
            if src:
                user32.SystemParametersInfoW(SPI_SETDESKWALLPAPER, 0, src, 0)
        for p in old:
            try:
                os.remove(p)
            except OSError:
                pass
        clean_history()

    def original_of(self, desk):
        now = current_wallpaper()
        if not is_ours(now):
            return now                      # 空字符串 = 纯色背景
        # 系统还显示着旧版本生成的图：用旧版本记下的原壁纸
        return self.originals.get(desk or "default") or ""

    def mtime(self):
        try:
            return os.path.getmtime(CONFIG_PATH)
        except OSError:
            return 0

    def base_for(self, src, w, h):
        # 文件的修改时间、契合度也算进去：幻灯片 / 聚焦壁纸可能一直用同一个路径换图
        path = usable_wallpaper(src)
        try:
            stamp = os.path.getmtime(path)
        except OSError:
            stamp = None
        key = (src, path, stamp, background_color(), reg_get(DESKTOP_KEY, "WallpaperStyle"),
               reg_get(DESKTOP_KEY, "TileWallpaper"), w, h)
        if key not in self.bases:
            img = load_wallpaper(src, w, h)
            if len(self.bases) > 6:
                self.bases.clear()
            self.bases[key] = img
        return key, self.bases[key]

    def preview(self, user):
        """设置窗口发来的「先看看效果」：只在内存里换设置，不写文件。"""
        cfg = normalize(user)
        cfg["autostart"] = self.cfg["autostart"]
        self.cfg = cfg

    def update(self):
        m = self.mtime()
        if m != self.cfg_mtime:
            self.cfg_mtime = m
            self.cfg = load_config(self.cfg)
            sync_autostart(self.cfg["autostart"])

        w, h = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        full = self.canvas.ensure(w, h)
        if not self.canvas.hwnd:
            return

        now = clock_now(self.cfg)
        text_key = (time_label(self.cfg, now), style_key(self.cfg), w, h)
        if text_key != self.text_key:
            self.text_img = render(self.cfg, w, h, now)
            self.text_key = text_key
        img, offset = self.text_img
        layer = (img, place(self.cfg, w, h, offset))
        text_changed = self.layer is None or layer[0] is not self.layer[0] or layer[1] != self.layer[1]
        self.layer = layer

        base_key, base = self.base_for(self.original_of(current_desktop()), w, h)
        if base_key != self.base_key or full or self.canvas.frame is None:
            # 换了桌面 / 换了壁纸 / 窗口重建：整屏重画
            self.canvas.frame = base.copy()
            self.drawn_box = None
            self.compose(base)
            self.base_key = base_key
            self.canvas.invalidate()
        elif text_changed:
            box = self.compose(base)
            self.canvas.invalidate(box)

    @staticmethod
    def box(img, pos):
        return (pos[0], pos[1], pos[0] + img.width, pos[1] + img.height)

    def compose(self, base):
        """只重画文字那一块：先把上次文字的位置还原成原壁纸，再把新文字贴上去。返回改动的范围。"""
        frame = self.canvas.frame
        img, pos = self.layer
        new = self.box(img, pos)
        if self.drawn_box:
            frame.paste(base.crop(self.drawn_box), self.drawn_box[:2])
        # 文字块可能有一部分在屏幕外，裁到屏幕范围内
        W_, H_ = frame.size
        cl = (max(0, new[0]), max(0, new[1]), min(W_, new[2]), min(H_, new[3]))
        if cl[2] > cl[0] and cl[3] > cl[1]:
            part = base.crop(cl).convert("RGBA")
            part.alpha_composite(img.crop((cl[0] - pos[0], cl[1] - pos[1],
                                           cl[2] - pos[0], cl[3] - pos[1])))
            frame.paste(part.convert("RGB"), cl[:2])
        old = self.drawn_box or cl
        self.drawn_box = cl
        return (min(old[0], cl[0]), min(old[1], cl[1]), max(old[2], cl[2]), max(old[3], cl[3]))

    def close(self):
        self.canvas.destroy()


# ---------------------------------------------------------------- 设置窗口
# 只有打开设置时才会加载 tkinter（单独一个进程），平时跑着的时钟不占这份内存。
TZ_CN = {
    "Asia/Shanghai": "上海 · 北京时间", "Asia/Hong_Kong": "香港", "Asia/Macau": "澳门",
    "Asia/Taipei": "台北", "Asia/Urumqi": "乌鲁木齐", "Asia/Tokyo": "东京 · 日本",
    "Asia/Seoul": "首尔 · 韩国", "Asia/Singapore": "新加坡", "Asia/Kuala_Lumpur": "吉隆坡 · 马来西亚",
    "Asia/Bangkok": "曼谷 · 泰国", "Asia/Ho_Chi_Minh": "胡志明市 · 越南", "Asia/Jakarta": "雅加达 · 印尼",
    "Asia/Manila": "马尼拉 · 菲律宾", "Asia/Kolkata": "印度", "Asia/Dubai": "迪拜 · 阿联酋",
    "Asia/Kathmandu": "加德满都 · 尼泊尔", "Asia/Karachi": "卡拉奇 · 巴基斯坦",
    "Asia/Tashkent": "塔什干 · 乌兹别克斯坦", "Asia/Vladivostok": "符拉迪沃斯托克 · 海参崴",
    "Europe/London": "伦敦 · 英国", "Europe/Dublin": "都柏林 · 爱尔兰", "Europe/Paris": "巴黎 · 法国",
    "Europe/Berlin": "柏林 · 德国", "Europe/Rome": "罗马 · 意大利", "Europe/Madrid": "马德里 · 西班牙",
    "Europe/Amsterdam": "阿姆斯特丹 · 荷兰", "Europe/Zurich": "苏黎世 · 瑞士",
    "Europe/Stockholm": "斯德哥尔摩 · 瑞典", "Europe/Helsinki": "赫尔辛基 · 芬兰",
    "Europe/Athens": "雅典 · 希腊", "Europe/Istanbul": "伊斯坦布尔 · 土耳其",
    "Europe/Moscow": "莫斯科 · 俄罗斯", "Europe/Kyiv": "基辅 · 乌克兰",
    "America/New_York": "纽约 · 美国东部", "America/Chicago": "芝加哥 · 美国中部",
    "America/Denver": "丹佛 · 美国山地", "America/Los_Angeles": "洛杉矶 · 美国西部",
    "America/Anchorage": "安克雷奇 · 阿拉斯加", "Pacific/Honolulu": "檀香山 · 夏威夷",
    "America/Toronto": "多伦多 · 加拿大", "America/Vancouver": "温哥华 · 加拿大",
    "America/Mexico_City": "墨西哥城", "America/Sao_Paulo": "圣保罗 · 巴西",
    "America/Argentina/Buenos_Aires": "布宜诺斯艾利斯 · 阿根廷", "America/Santiago": "圣地亚哥 · 智利",
    "Australia/Sydney": "悉尼 · 澳大利亚", "Australia/Melbourne": "墨尔本 · 澳大利亚",
    "Australia/Perth": "珀斯 · 澳大利亚", "Pacific/Auckland": "奥克兰 · 新西兰",
    "Africa/Cairo": "开罗 · 埃及", "Africa/Johannesburg": "约翰内斯堡 · 南非",
    "Africa/Nairobi": "内罗毕 · 肯尼亚", "Africa/Lagos": "拉各斯 · 尼日利亚", "UTC": "协调世界时",
}

FONT_CN = {
    "msyh.ttc": "微软雅黑", "msyhl.ttc": "微软雅黑 细体", "msyhbd.ttc": "微软雅黑 粗体",
    "deng.ttf": "等线", "dengl.ttf": "等线 细体", "dengb.ttf": "等线 粗体",
    "simhei.ttf": "黑体", "simsun.ttc": "宋体", "simsunb.ttf": "宋体 扩展", "simkai.ttf": "楷体",
    "simfang.ttf": "仿宋", "simli.ttf": "隶书", "simyou.ttf": "幼圆", "stxingka.ttf": "华文行楷",
    "stkaiti.ttf": "华文楷体", "stsong.ttf": "华文宋体", "stxihei.ttf": "华文细黑",
    "msjh.ttc": "微軟正黑體", "mingliub.ttc": "細明體",
}

COLOR_PRESETS = [("#ffffff", "白色"), ("#f3ece0", "奶油白"), ("#d97757", "小克橙"),
                 ("#ffc4d6", "樱花粉"), ("#a8d8ff", "天空蓝"), ("#b8e6c1", "薄荷绿"),
                 ("#ffe08a", "柠檬黄"), ("#1f1f1f", "黑色")]


def norm_path(p):
    return os.path.normcase(os.path.normpath(p)) if p else ""


def utc_text(td):
    m = int(td.total_seconds() // 60)
    sign = "+" if m >= 0 else "-"
    m = abs(m)
    return f"UTC{sign}{m // 60:02d}:{m % 60:02d}", f"utc{sign}{m // 60}" + (f":{m % 60:02d}" if m % 60 else "")


def timezone_items(rest=False, extra=None):
    """(值, 显示的几列, 搜索用的文字)。

    rest=False：「跟随电脑」+ 带中文名的常用城市（+ 正在用的那个），很快，打开窗口时用；
    rest=True：其余几百个时区，要读几百个文件，等窗口出来以后再在后台补上。
    """
    now = datetime.now(timezone.utc)

    def item(name, cn=None):
        z = tz_of(name)
        if not z:
            return None
        full, short = utc_text(now.astimezone(z).utcoffset())
        cols = (cn, name, full) if cn else (name.replace("_", " "), "", full)
        return name, cols, f"{' '.join(cols)} {name} {short}".lower()

    if rest:
        if rest is True:
            from zoneinfo import available_timezones
            rest = sorted(n for n in available_timezones() if "/" in n and n not in TZ_CN and n != extra
                          and not n.startswith(("Etc/", "SystemV/", "US/", "posix", "right/")))
        return [it for it in (item(n) for n in rest) if it]
    local = datetime.now().astimezone().utcoffset()
    full, short = utc_text(local)
    items = [("local", ("跟随电脑的时区", "", full), f"跟随电脑 系统 本地 local {full} {short}".lower())]
    popular = [it for it in (item(n, cn) for n, cn in TZ_CN.items()) if it]
    popular.sort(key=lambda it: (now.astimezone(tz_of(it[0])).utcoffset(), it[0]))
    items += popular
    if extra and extra != "local" and extra not in TZ_CN:
        items += [it for it in (item(extra),) if it]
    return items


def font_items():
    """装在这台电脑上的字体：(文件路径, 显示的几列, 搜索用的文字)。中文字体排在前面。"""
    found = {}
    key = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"
    places = ((winreg.HKEY_LOCAL_MACHINE, os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")),
              (winreg.HKEY_CURRENT_USER, os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Microsoft\Windows\Fonts")))
    for hive, folder in places:
        try:
            k = winreg.OpenKey(hive, key)
        except OSError:
            continue
        with k:
            i = 0
            while True:
                try:
                    name, file, _ = winreg.EnumValue(k, i)
                except OSError:
                    break
                i += 1
                if not isinstance(file, str):
                    continue
                path = file if os.path.isabs(file) else os.path.join(folder, file)
                if os.path.splitext(path)[1].lower() not in (".ttf", ".ttc", ".otf"):
                    continue
                name = re.sub(r"\s*\((TrueType|OpenType)\)\s*$", "", name)
                found.setdefault(norm_path(path), (path.replace("\\", "/"), name))
    defaults = (norm_path(DEFAULT_CONFIG["time_font"]), norm_path(DEFAULT_CONFIG["text_font"]))
    items = []
    for path, name in found.values():
        base = os.path.basename(path).lower()
        cn = FONT_CN.get(base)
        mark = "默认" if norm_path(path) in defaults else ""
        cols = (cn, name, mark) if cn else (name, "", mark)
        items.append((path, cols, f"{' '.join(cols)} {base}".lower(), cn is None))
    items.sort(key=lambda it: (it[3], it[1][0].lower()))
    return [it[:3] for it in items]


def wallpaper_stamp():
    src = usable_wallpaper(current_wallpaper())
    try:
        return src, os.path.getmtime(src), reg_get(DESKTOP_KEY, "WallpaperStyle")
    except OSError:
        return src, background_color()


def wallpaper_image(w, h):
    return load_wallpaper(current_wallpaper(), w, h)


# ---- 设置窗口的样子：Win11 风格的浅色界面，小克橙作强调色
BG, CARD, LINE, TEXT, SUB, FAINT = "#f3f3f3", "#ffffff", "#e6e6e6", "#1a1a1a", "#6e6e6e", "#9b9b9b"
ACCENT, ACCENT_HOVER, ACCENT_DOWN, SOFT = "#d97757", "#cb6a4b", "#b95e42", "#fbe8e1"
UI_FONT = "Microsoft YaHei UI"
TAB_NAMES = ("时间", "位置和大小", "颜色", "字体", "其它")


def hex_rgb(hx):
    return tuple(int(hx[i:i + 2], 16) for i in (1, 3, 5))


def smooth(w, h, draw, bg):
    """先放大 4 倍画再缩小：圆角、圆点的边缘都是平滑的。"""
    ss = 4
    big = Image.new("RGB", (w * ss, h * ss), hex_rgb(bg))
    draw(ImageDraw.Draw(big), ss)
    return big.resize((w, h), Image.LANCZOS)


def round_box(w, h, r, fill, bg, outline=None, width=1):
    def draw(d, ss):
        d.rounded_rectangle((0, 0, w * ss - 1, h * ss - 1), r * ss, fill=fill,
                            outline=outline, width=width * ss if outline else 0)
    return smooth(w, h, draw, bg)


def setup_theme(root, P):
    """所有控件的样式。圆角都是用图片拼出来的（ttk 的「九宫格」图片元素）。"""
    st = ttk.Style(root)
    st.theme_use("clam")
    keep = []

    def photo(img):
        keep.append(ImageTk.PhotoImage(img))
        return keep[-1]

    hair = max(1, P(1))
    st.configure(".", background=CARD, foreground=TEXT, font=(UI_FONT, 10), borderwidth=0, focuscolor=CARD)
    st.configure("TFrame", background=CARD)
    st.configure("BG.TFrame", background=BG)
    st.configure("Line.TFrame", background=LINE)
    st.configure("TLabel", background=CARD, foreground=TEXT)
    st.configure("Sub.TLabel", background=CARD, foreground=SUB, font=(UI_FONT, 9))
    st.configure("Title.TLabel", background=CARD, foreground=TEXT, font=(UI_FONT, 10, "bold"))
    st.configure("SubBG.TLabel", background=BG, foreground=SUB, font=(UI_FONT, 9))

    # 拼圆角用的图片：四个角不动，中间那块会被重复铺满，所以中间留大一点，铺的次数少、画得快
    r = P(5)
    n = 2 * r + 64

    def button(name, surface, fills, outline, fg):
        imgs = [photo(round_box(n, n, r, c, surface, outline, hair)) for c in fills]
        st.element_create(f"{name}.bg", "image", imgs[0], ("pressed", imgs[2]), ("active", imgs[1]),
                          border=r + 1, sticky="nsew")
        st.layout(f"{name}.TButton", [(f"{name}.bg", {"sticky": "nsew", "children": [
            ("Button.padding", {"sticky": "nsew", "children": [("Button.label", {"sticky": "nsew"})]})]})])
        st.configure(f"{name}.TButton", padding=(P(14), P(5)), foreground=fg, anchor="center")

    button("Pill", CARD, ("#fbfbfb", "#f4f4f4", "#ececec"), "#d4d4d4", TEXT)
    button("PillBG", BG, ("#fdfdfd", "#f7f7f7", "#eeeeee"), "#d4d4d4", TEXT)
    button("Accent", BG, (ACCENT, ACCENT_HOVER, ACCENT_DOWN), None, "#ffffff")
    button("Chosen", CARD, (SOFT, SOFT, "#f6d9ce"), "#e9b29e", "#a24c2c")

    field = [photo(round_box(n, n, r, CARD, CARD, c, w)) for c, w in (("#d4d4d4", hair), (ACCENT, hair * 2))]
    st.element_create("Round.field", "image", field[0], ("focus", field[1]), border=r + 2, sticky="nsew")
    st.layout("Round.TEntry", [("Round.field", {"sticky": "nsew", "children": [
        ("Entry.padding", {"sticky": "nsew", "children": [("Entry.textarea", {"sticky": "nsew"})]})]})])
    st.configure("Round.TEntry", padding=(P(9), P(5)), foreground=TEXT, insertcolor=TEXT,
                 selectbackground=SOFT, selectforeground=TEXT, fieldbackground=CARD)

    rc = P(8)
    m = 2 * rc + 160
    st.element_create("Card.bg", "image", photo(round_box(m, m, rc, CARD, BG, LINE, hair)),
                      border=rc + 1, sticky="nsew")
    st.layout("Card.TFrame", [("Card.bg", {"sticky": "nsew"})])
    st.element_create("Box.bg", "image", photo(round_box(n, n, r, CARD, CARD, "#dcdcdc", hair)),
                      border=r + 1, sticky="nsew")
    st.layout("Box.TFrame", [("Box.bg", {"sticky": "nsew"})])

    st.configure("List.Treeview", background=CARD, fieldbackground=CARD, foreground=TEXT,
                 rowheight=P(30), borderwidth=0, font=(UI_FONT, 10))
    st.layout("List.Treeview", [("Treeview.treearea", {"sticky": "nsew"})])
    st.layout("Treeview.Item", [("Treeitem.padding", {"sticky": "nswe", "children": [
        ("Treeitem.text", {"side": "left", "sticky": ""})]})])
    st.map("List.Treeview", background=[("selected", SOFT)], foreground=[("selected", TEXT)])

    st.layout("Thin.Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
        ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
    st.configure("Thin.Vertical.TScrollbar", troughcolor=CARD, background="#d2d2d2", bordercolor=CARD,
                 lightcolor="#d2d2d2", darkcolor="#d2d2d2", arrowsize=P(6), width=P(6), gripcount=0)
    st.map("Thin.Vertical.TScrollbar", background=[("active", "#b4b4b4")],
           lightcolor=[("active", "#b4b4b4")], darkcolor=[("active", "#b4b4b4")])
    return keep


class LOGFONTW(ctypes.Structure):
    _fields_ = [("lfHeight", W.LONG), ("lfWidth", W.LONG), ("lfEscapement", W.LONG),
                ("lfOrientation", W.LONG), ("lfWeight", W.LONG), ("lfItalic", W.BYTE),
                ("lfUnderline", W.BYTE), ("lfStrikeOut", W.BYTE), ("lfCharSet", W.BYTE),
                ("lfOutPrecision", W.BYTE), ("lfClipPrecision", W.BYTE), ("lfQuality", W.BYTE),
                ("lfPitchAndFamily", W.BYTE), ("lfFaceName", W.WCHAR * 32)]


def ime_font(widget, pt=10):
    """输入框里打拼音时显示的那串字母：Tk 不告诉输入法用多大的字，高分屏上就会特别小。这里告诉它。"""
    try:
        imm = ctypes.WinDLL("imm32")
        imm.ImmGetContext.restype = W.HANDLE
        imm.ImmGetContext.argtypes = [W.HWND]
        imm.ImmReleaseContext.argtypes = [W.HWND, W.HANDLE]
        imm.ImmSetCompositionFontW.argtypes = [W.HANDLE, ctypes.POINTER(LOGFONTW)]
        lf = LOGFONTW()
        lf.lfHeight = -round(pt * widget.winfo_fpixels("1i") / 72)
        lf.lfWeight = 400
        lf.lfCharSet = 1                      # DEFAULT_CHARSET
        lf.lfQuality = 5                      # CLEARTYPE_QUALITY
        lf.lfFaceName = UI_FONT
        user32.GetFocus.restype = W.HWND
        user32.GetParent.restype = W.HWND
        user32.GetParent.argtypes = [W.HWND]
        hwnds = {widget.winfo_id(), user32.GetFocus()}
        hwnds.add(user32.GetParent(widget.winfo_id()))
        for h in hwnds:
            if not h:
                continue
            himc = imm.ImmGetContext(h)
            if himc:
                imm.ImmSetCompositionFontW(himc, ctypes.byref(lf))
                imm.ImmReleaseContext(h, himc)
    except Exception as e:
        log(f"设置输入法字体失败：{e!r}")


def set_window_icons(root):
    """标题栏、任务栏的图标：从 ico 里挑正好合适大小的那张，高分屏上也是清楚的。"""
    try:
        hwnd = int(root.wm_frame(), 16)
        dpi = user32.GetDpiForWindow(hwnd) or 96
        user32.SendMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
        user32.GetSystemMetricsForDpi.argtypes = [ctypes.c_int, W.UINT]
        for which, metric in ((0, 49), (1, 11)):          # ICON_SMALL / SM_CXSMICON，ICON_BIG / SM_CXICON
            size = user32.GetSystemMetricsForDpi(metric, dpi)
            h = user32.LoadImageW(None, ICON_PATH, 1, size, size, 0x10)   # IMAGE_ICON, LR_LOADFROMFILE
            if h:
                user32.SendMessageW(hwnd, 0x0080, which, h)              # WM_SETICON
    except Exception as e:
        log(f"设置窗口图标失败：{e!r}")


class Toggle:
    """Win11 风格的开关。"""

    def __init__(self, parent, P, command):
        self.command, self.on, self.hover = command, False, False
        self.w, self.h = P(40), P(20)
        self.cv = tk.Canvas(parent, width=self.w, height=self.h, bg=CARD, highlightthickness=0, bd=0,
                            cursor="hand2")
        self.imgs = {(on, hv): ImageTk.PhotoImage(self.paint(on, hv)) for on in (False, True)
                     for hv in (False, True)}
        self.item = self.cv.create_image(0, 0, anchor="nw")
        self.cv.bind("<Button-1>", lambda e: self.click())
        self.cv.bind("<Enter>", lambda e: self.set_hover(True))
        self.cv.bind("<Leave>", lambda e: self.set_hover(False))
        self.draw()

    def paint(self, on, hv):
        w, h = self.w, self.h

        def draw(d, ss):
            W_, H_ = w * ss, h * ss
            r = H_ / 2
            if on:
                d.rounded_rectangle((0, 0, W_ - 1, H_ - 1), r, fill=ACCENT_HOVER if hv else ACCENT)
                k = H_ * (0.32 if hv else 0.3)
                cx = W_ - r
                d.ellipse((cx - k, r - k, cx + k, r + k), fill="#ffffff")
            else:
                d.rounded_rectangle((0, 0, W_ - 1, H_ - 1), r, fill="#f0f0f0" if hv else CARD,
                                    outline="#8a8a8a", width=max(ss, round(H_ * 0.07)))
                k = H_ * (0.26 if hv else 0.22)
                d.ellipse((r - k, r - k, r + k, r + k), fill="#5a5a5a")
        return smooth(w, h, draw, CARD)

    def set(self, on):
        self.on = bool(on)
        self.draw()

    def set_hover(self, hv):
        self.hover = hv
        self.draw()

    def click(self):
        self.on = not self.on
        self.draw()
        self.command(self.on)

    def draw(self):
        self.cv.itemconfigure(self.item, image=self.imgs[self.on, self.hover])


class Slider:
    """Win11 风格的滑块：一条细轨道 + 圆形把手。"""

    def __init__(self, parent, P, lo, hi, command, length):
        self.P, self.lo, self.hi, self.command = P, lo, hi, command
        self.w, self.h, self.pad = length, P(24), P(11)
        self.v, self.hover, self.photo = lo, False, None
        self.cv = tk.Canvas(parent, width=self.w, height=self.h, bg=CARD, highlightthickness=0, bd=0,
                            cursor="hand2")
        self.item = self.cv.create_image(0, 0, anchor="nw")
        self.cv.bind("<Button-1>", self.drag)
        self.cv.bind("<B1-Motion>", self.drag)
        self.cv.bind("<Enter>", lambda e: self.set_hover(True))
        self.cv.bind("<Leave>", lambda e: self.set_hover(False))

    def set(self, v):
        self.v = v
        self.draw()

    def set_hover(self, hv):
        self.hover = hv
        self.draw()

    def drag(self, e):
        f = min(1.0, max(0.0, (e.x - self.pad) / (self.w - 2 * self.pad)))
        self.v = self.lo + f * (self.hi - self.lo)
        self.draw()
        self.command(self.v)

    def draw(self):
        f = (min(self.hi, max(self.lo, self.v)) - self.lo) / (self.hi - self.lo)
        x = self.pad + f * (self.w - 2 * self.pad)
        w, h, pad, P = self.w, self.h, self.pad, self.P

        def paint(d, ss):
            cy = h * ss / 2
            t = max(ss, P(2) * ss) / 2 + ss / 2
            d.rounded_rectangle((pad * ss, cy - t, (w - pad) * ss, cy + t), t, fill="#c6c6c6")
            d.rounded_rectangle((pad * ss, cy - t, x * ss, cy + t), t, fill=ACCENT)
            R = P(10) * ss
            d.ellipse((x * ss - R, cy - R, x * ss + R, cy + R), fill="#ffffff", outline="#d6d6d6",
                      width=max(ss, P(1) * ss))
            k = P(6 if self.hover else 5) * ss
            d.ellipse((x * ss - k, cy - k, x * ss + k, cy + k), fill=ACCENT)
        self.photo = ImageTk.PhotoImage(smooth(w, h, paint, CARD))
        self.cv.itemconfigure(self.item, image=self.photo)


class Swatch:
    """一个圆形色块；选中时外面多一圈。"""

    def __init__(self, parent, P, hx, name, command):
        self.size = P(28)
        self.cv = tk.Canvas(parent, width=self.size, height=self.size, bg=CARD, highlightthickness=0,
                            bd=0, cursor="hand2")
        self.imgs = [ImageTk.PhotoImage(self.paint(hx, sel, P)) for sel in (False, True)]
        self.item = self.cv.create_image(0, 0, anchor="nw", image=self.imgs[0])
        self.cv.bind("<Button-1>", lambda e: command(hx))
        Tip(self.cv, name)

    def paint(self, hx, selected, P):
        s = self.size

        def draw(d, ss):
            S_ = s * ss
            c, k = S_ / 2, S_ * 0.34
            if selected:
                ring = max(ss * 2, P(2) * ss)
                d.ellipse((ss, ss, S_ - ss, S_ - ss), outline=TEXT, width=ring)
            d.ellipse((c - k, c - k, c + k, c + k), fill=hx, outline="#cfcfcf", width=max(ss, P(1) * ss) // 2)
        return smooth(s, s, draw, CARD)

    def select(self, on):
        self.cv.itemconfigure(self.item, image=self.imgs[1 if on else 0])


class SearchList:
    """上面一个搜索框、下面一个列表。打字就会筛选，点一下就选中。

    items：(值, 显示的几列, 搜索用的文字)；columns：每列 (宽度, 对齐, 能不能拉宽)
    """

    def __init__(self, parent, P, items, on_pick, columns, placeholder, height=6):
        self.items = list(items)
        self.on_pick = on_pick
        self.value = None
        self.shown = []
        self.frame = ttk.Frame(parent)
        self.query = tk.StringVar()
        self.entry = ttk.Entry(self.frame, textvariable=self.query, style="Round.TEntry")
        self.entry.pack(fill="x")
        # 搜索框里灰色的提示字，开始打字就消失
        self.hint = tk.Label(self.entry, text=placeholder, bg=CARD, fg=FAINT, font=(UI_FONT, 10),
                             cursor="xterm", bd=0, padx=0, pady=0)
        self.hint.place(x=P(10), rely=0.5, anchor="w")
        self.hint.bind("<Button-1>", lambda e: self.entry.focus_set())
        # 点进去就把灰字收起来，打拼音的时候不会和它叠在一起
        self.entry.bind("<FocusIn>", lambda e: self.hint.place_forget(), add="+")
        self.entry.bind("<FocusOut>", lambda e: self.show_hint(), add="+")
        box = ttk.Frame(self.frame, style="Box.TFrame")
        box.pack(fill="x", pady=(P(8), 0))
        names = [f"c{i}" for i in range(len(columns))]
        self.tv = ttk.Treeview(box, style="List.Treeview", columns=names, show="", height=height,
                               selectmode="browse")
        for name, (width, anchor, stretch) in zip(names, columns):
            self.tv.column(name, width=width, minwidth=0, anchor=anchor, stretch=stretch)
        sb = ttk.Scrollbar(box, orient="vertical", command=self.tv.yview, style="Thin.Vertical.TScrollbar")
        self.tv.configure(yscrollcommand=sb.set)
        self.tv.pack(side="left", fill="both", expand=True, padx=(P(5), 0), pady=P(5))
        sb.pack(side="left", fill="y", padx=P(4), pady=P(8))
        self.query.trace_add("write", lambda *_: self.refilter())
        self.tv.bind("<<TreeviewSelect>>", self._clicked)
        self.entry.bind("<Return>", self._enter)
        self.entry.bind("<Down>", self._down)
        self.refilter()

    def refilter(self):
        q = self.query.get()
        self.show_hint()
        words = q.lower().split()
        self.shown = [it for it in self.items if all(w in it[2] for w in words)]
        self.tv.delete(*self.tv.get_children())
        for i, it in enumerate(self.shown):
            self.tv.insert("", "end", iid=str(i), values=["  " + c if c and j == 0 else c
                                                          for j, c in enumerate(it[1])])
        self._mark()

    def show_hint(self):
        try:
            focused = self.entry.focus_get() is self.entry
        except Exception:
            focused = False
        if self.query.get() or focused:
            self.hint.place_forget()
        else:
            self.hint.place(x=self._hint_x(), rely=0.5, anchor="w")

    def _hint_x(self):
        return int(self.entry.winfo_fpixels("1i") / 96 * 10)

    def _mark(self):
        for i, it in enumerate(self.shown):
            if norm_path(it[0]) == norm_path(self.value):
                self.tv.selection_set(str(i))
                self.tv.see(str(i))
                return
        self.tv.selection_set(())

    def extend(self, more):
        """后台加载完的条目补到列表末尾。"""
        self.items += more
        if self.query.get():
            self.refilter()
        else:
            base = len(self.shown)
            self.shown += more
            for i, it in enumerate(more, base):
                self.tv.insert("", "end", iid=str(i), values=["  " + c if c and j == 0 else c
                                                              for j, c in enumerate(it[1])])

    def cols_of(self, value):
        for it in self.items:
            if norm_path(it[0]) == norm_path(value):
                return it[1]
        return None

    def set(self, value, fallback_cols=None):
        if self.cols_of(value) is None and fallback_cols:
            self.items.insert(0, (value, fallback_cols, " ".join(fallback_cols).lower()))
            self.refilter()
        self.value = value
        self._mark()

    def _pick(self, value):
        self.value = value
        self._mark()
        self.on_pick(value)

    def _clicked(self, _):
        sel = self.tv.selection()
        if sel:
            v = self.shown[int(sel[0])][0]
            if norm_path(v) != norm_path(self.value):
                self._pick(v)

    def _enter(self, _):
        if self.shown:
            self._pick(self.shown[0][0])

    def _down(self, _):
        self.tv.focus_set()
        if self.shown:
            first = self.tv.selection() or ("0",)
            self.tv.focus(first[0])
            if not self.tv.selection():
                self.tv.selection_set("0")


class SettingsApp:
    def __init__(self):
        self.root = root = tk.Tk()
        root.withdraw()
        root.title(SETTINGS_TITLE)
        try:
            root.iconbitmap(default=ICON_PATH)   # 之后弹出的小窗口（选颜色、选文件……）也用小克图标
        except Exception as e:
            log(f"设置窗口图标加载失败：{e!r}")
        root.resizable(False, False)
        root.configure(bg=BG)
        self.S = S = root.winfo_fpixels("1i") / 96          # 屏幕缩放（150% = 1.5）
        P = lambda v: int(round(v * S))
        self.P = P
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            tkfont.nametofont(name).configure(family=UI_FONT, size=10)
        self.keep = setup_theme(root, P)
        # 所有输入框：拼音用正常大小的字
        root.bind_class("TEntry", "<FocusIn>", lambda e: ime_font(e.widget), add="+")

        self.cfg = load_config()
        self.original = dict(self.cfg)
        self.W, self.H = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        self.loading = False
        self.setters = []
        self.send_job = None
        self.draw_job = None
        self.dragging = False
        self.snap = (False, False)
        self.grab = (0, 0)

        outer = ttk.Frame(root, style="BG.TFrame", padding=(P(24), P(18), P(24), P(20)))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)

        # ---- 第一行：左边「预览」，右边各个页面的标签
        self.heading(outer, "预览").grid(row=0, column=0, sticky="sw", padx=(0, P(24)))
        bar = ttk.Frame(outer, style="BG.TFrame")
        bar.grid(row=0, column=1, sticky="sw")
        self.tabs = {}
        for name in TAB_NAMES:
            box, lab, line = self.heading(bar, name, clickable=True)
            box.pack(side="left", padx=(0, P(22)))
            lab.bind("<Button-1>", lambda e, n=name: self.show_page(n))
            lab.bind("<Enter>", lambda e, l=lab: l.configure(fg=TEXT))
            lab.bind("<Leave>", lambda e, n=name: self.paint_tabs())
            self.tabs[name] = (lab, line)

        # ---- 左边：预览（可以直接拖）+ 两个按钮
        left = ttk.Frame(outer, style="BG.TFrame")
        left.grid(row=1, column=0, sticky="nw", padx=(0, P(24)), pady=(P(12), 0))
        self.pw = P(420)
        self.ph = int(self.pw * self.H / self.W)
        self.pv_stamp = None
        self.pv_key = None
        self.pv_mask = smooth(self.pw, self.ph, lambda d, ss: d.rounded_rectangle(
            (0, 0, self.pw * ss - 1, self.ph * ss - 1), P(8) * ss, fill="#ffffff"), "#000000").convert("L")
        self.pv = tk.Canvas(left, width=self.pw, height=self.ph, highlightthickness=0, bd=0, bg=BG,
                            cursor="fleur")
        self.pv.pack(anchor="w")
        self.pv_item = self.pv.create_image(0, 0, anchor="nw")
        dash = dict(fill=ACCENT, dash=(4, 3), width=max(1, P(1)), state="hidden")
        self.pv_gx = self.pv.create_line(self.pw // 2, 0, self.pw // 2, self.ph, **dash)
        self.pv_gy = self.pv.create_line(0, self.ph // 2, self.pw, self.ph // 2, **dash)
        self.pv.bind("<ButtonPress-1>", self.pv_press)
        self.pv.bind("<B1-Motion>", self.pv_move)
        self.pv.bind("<ButtonRelease-1>", self.pv_release)
        self.status = ttk.Label(left, style="SubBG.TLabel", wraplength=self.pw, justify="left")
        self.status.pack(anchor="w", pady=(P(10), 0))
        btns = ttk.Frame(left, style="BG.TFrame")
        btns.pack(anchor="w", pady=(P(14), 0))
        ttk.Button(btns, text="全屏预览编辑…", style="PillBG.TButton",
                   command=self.pick_fullscreen).pack(side="left")
        ttk.Button(btns, text="放回正中间", style="PillBG.TButton",
                   command=lambda: self.set_center(0.5, 0.5)).pack(side="left", padx=(P(8), 0))

        # ---- 右边：白色卡片，里面是当前页面
        card = ttk.Frame(outer, style="Card.TFrame", padding=(P(20), P(6)))
        card.grid(row=1, column=1, sticky="nsew", pady=(P(12), 0))
        card.columnconfigure(0, weight=1)
        card.rowconfigure(0, weight=1)
        self.pages = {}
        self.wraps = []
        for name, build in zip(TAB_NAMES, (self.build_time, self.build_position, self.build_color,
                                           self.build_fonts, self.build_other)):
            page = ttk.Frame(card)
            page.grid(row=0, column=0, sticky="nsew")
            build(page)
            self.pages[name] = page
        self.page = TAB_NAMES[0]

        # ---- 最下面一行
        foot = ttk.Frame(outer, style="BG.TFrame")
        foot.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(P(20), 0))
        ttk.Button(foot, text="恢复默认", style="PillBG.TButton", command=self.reset).pack(side="left")
        ttk.Button(foot, text="完成", style="Accent.TButton", command=self.finish).pack(side="right")
        ttk.Button(foot, text="取消", style="PillBG.TButton", command=self.cancel).pack(
            side="right", padx=(0, P(8)))

        root.protocol("WM_DELETE_WINDOW", self.finish)
        root.bind("<Escape>", lambda e: self.cancel())
        self.refresh_widgets()
        self.show_page(self.page)
        self.draw_preview()
        self.tick()

        root.update_idletasks()
        self.fit_wraps(card)
        root.update_idletasks()
        w, h = root.winfo_reqwidth(), root.winfo_reqheight()
        root.geometry(f"+{max(0, (self.W - w) // 2)}+{max(0, (self.H - h) // 2 - P(20))}")
        root.deiconify()
        set_window_icons(root)
        root.lift()
        root.focus_force()
        # 列表要等窗口排好版以后才能滚到选中的那一项
        root.after(80, lambda: (self.tz_list._mark(), self.font_list._mark()))
        # 冷门时区等窗口出来以后再补上（要读几百个小文件），打开窗口不用等它
        root.after(150, self.load_more_timezones)

    def load_more_timezones(self, names=None):
        """一次补 40 个，中间让窗口喘口气，拖动、打字都不会卡。"""
        if names is None:
            from zoneinfo import available_timezones
            extra = self.cfg["main_tz"]
            names = sorted(n for n in available_timezones() if "/" in n and n not in TZ_CN and n != extra
                           and not n.startswith(("Etc/", "SystemV/", "US/", "posix", "right/")))
        if names:
            self.tz_list.extend(timezone_items(rest=names[:40]))
            self.root.after(1, self.load_more_timezones, names[40:])

    # ---------------------------------------------------------- 小零件
    def heading(self, parent, text, clickable=False):
        """一行标题文字 + 下面一条细线（选中的页面是橙色）。预览的标题也用它，两边就对得齐。"""
        box = tk.Frame(parent, bg=BG)
        lab = tk.Label(box, text=text, bg=BG, fg=SUB if clickable else TEXT, font=(UI_FONT, 11),
                       bd=0, padx=0, pady=0, cursor="hand2" if clickable else "")
        lab.pack(anchor="w")
        line = tk.Frame(box, bg=BG, height=self.P(3))
        line.pack(fill="x", pady=(self.P(6), 0))
        return (box, lab, line) if clickable else box

    def show_page(self, name):
        self.page = name
        self.pages[name].tkraise()
        self.paint_tabs()
        if name == "字体":
            self.root.after(10, self.font_list._mark)
        elif name == "时间":
            self.root.after(10, self.tz_list._mark)

    def paint_tabs(self):
        for name, (lab, line) in self.tabs.items():
            on = name == self.page
            lab.configure(fg=TEXT if on else SUB)
            line.configure(bg=ACCENT if on else BG)

    def row(self, page, label=None, hint=None):
        """页面里的一行：左边名字、右边控件；行和行之间一条细线，每行一样高。"""
        if page.winfo_children():
            ttk.Frame(page, style="Line.TFrame", height=1).pack(fill="x")
        r = ttk.Frame(page)
        r.pack(fill="x", pady=self.P(9))
        r.columnconfigure(0, minsize=self.P(100))
        r.columnconfigure(1, weight=1)
        r.rowconfigure(0, minsize=self.P(34))
        if label:
            ttk.Label(r, text=label).grid(row=0, column=0, sticky="w")
        if hint:
            self.wrap_label(r, hint).grid(row=1, column=0, columnspan=4, sticky="ew", pady=(self.P(2), 0))
        return r

    def wrap_label(self, parent, text, style="Sub.TLabel"):
        """会自动按所在那一行的宽度换行的说明文字（不会提前在半中间断开）。"""
        # 先给一个窄的宽度，等整个窗口排好版、知道卡片多宽以后再一次性放宽（见 fit_wraps）
        # Tk 只在空格处换行，一整串中文算一个「词」：中文里夹一个英文单词，就会在那个空格处提前断行。
        # 换成不断行的空格，整段就按宽度一个字一个字地换行，和普通中文排版一样。
        lab = ttk.Label(parent, text=text.replace(" ", " "), style=style, justify="left",
                        wraplength=self.P(300))
        self.wraps.append(lab)
        return lab

    def fit_wraps(self, card):
        width = card.winfo_reqwidth() - 2 * self.P(20) - self.P(4)
        for lab in self.wraps:
            lab.configure(wraplength=width)

    def toggle_row(self, page, label, key, hint=None):
        r = self.row(page, label, hint)

        def on(v):
            self.cfg[key] = v
            self.changed()
        t = Toggle(r, self.P, on)
        t.cv.grid(row=0, column=1, columnspan=3, sticky="e")
        r.winfo_children()[0].bind("<Button-1>", lambda e: t.click())
        self.setters.append(lambda: t.set(self.cfg[key]))

    def number_row(self, page, label, key, lo, hi, unit, scale=1.0, step=1.0, half=False, limits=None,
                   hint=None, spec=None):
        """滑块 + 可以直接输入的数字框（回车确认；↑↓ 或滚轮一格一格调）。

        spec：可以换单位的行（字号）用它，每次都现问 (滑块最小, 最大, 能输入的范围, 精确到 0.5, 单位)。
        """
        r = self.row(page, label, hint)
        var = tk.StringVar()
        spec = spec or (lambda: (lo, hi, limits or (lo, hi), half, unit))

        def fmt(v):
            return f"{round(v * 2) / 2:g}" if spec()[3] else f"{round(v):d}"

        def apply(v, from_slider=False):
            _, _, (a, b), hf, _ = spec()
            v = min(b, max(a, v))
            v = round(v * 2) / 2 if hf else round(v)
            self.cfg[key] = round(v / scale, 4)
            if not from_slider:
                sl.set(v)
            var.set(fmt(v))
            self.changed()

        sl = Slider(r, self.P, lo, hi, lambda v: apply(v, True), self.P(196))
        sl.cv.grid(row=0, column=1, sticky="w")
        e = ttk.Entry(r, textvariable=var, width=6, style="Round.TEntry", justify="right")
        e.grid(row=0, column=2, sticky="e", padx=(self.P(10), self.P(8)))
        unit_lab = ttk.Label(r, style="Sub.TLabel", width=2)
        unit_lab.grid(row=0, column=3, sticky="w")

        def commit(_=None):
            try:
                apply(float(var.get().replace("，", ".").replace(",", ".").strip()))
            except ValueError:
                var.set(fmt(self.cfg[key] * scale))

        def nudge(sign):
            apply(self.cfg[key] * scale + sign * step)
            return "break"
        e.bind("<Return>", commit)
        e.bind("<FocusOut>", commit)
        e.bind("<Up>", lambda ev: nudge(1))
        e.bind("<Down>", lambda ev: nudge(-1))
        e.bind("<MouseWheel>", lambda ev: nudge(1 if ev.delta > 0 else -1))

        def load():
            sl.lo, sl.hi, _, _, u = spec()
            unit_lab.configure(text=u)
            v = self.cfg[key] * scale
            sl.set(v)
            var.set(fmt(v))
        self.setters.append(load)

    # ---------------------------------------------------------- 各个页面
    def build_time(self, f):
        P = self.P
        r = self.row(f, "时区")
        self.tz_now = ttk.Label(r, style="Title.TLabel")
        self.tz_now.grid(row=0, column=1, columnspan=3, sticky="w")
        self.tz_sub = ttk.Label(r, style="Sub.TLabel")
        self.tz_sub.grid(row=1, column=1, columnspan=3, sticky="w")
        r = self.row(f)
        self.tz_list = SearchList(r, P, timezone_items(extra=self.cfg["main_tz"]), self.pick_tz,
                                  [(P(150), "w", True), (P(150), "w", True), (P(90), "e", False)],
                                  "搜索城市或时区")
        self.tz_list.frame.grid(row=0, column=0, columnspan=4, sticky="ew")
        self.toggle_row(f, "显示秒数", "show_seconds")
        self.toggle_row(f, "显示日期和星期", "show_date")
        self.setters.append(lambda: self.tz_list.set(self.cfg["main_tz"], (self.cfg["main_tz"], "自定义", "")))

    def build_position(self, f):
        self.number_row(f, "左右", "center_x", 0, 100, "%", scale=100)
        self.number_row(f, "上下", "center_y", 0, 100, "%", scale=100,
                        hint="0% 是最左 / 最上，50% 是正中间。拖动预览时靠近正中间会自动吸附。")
        P = self.P
        r = self.row(f, "字号单位", hint="磅：和 Word 一样，跟着系统的屏幕缩放变大变小。"
                                        "像素：绝对大小，不管缩放是多少都一样。")
        box = ttk.Frame(r)
        box.grid(row=0, column=1, columnspan=3, sticky="w")
        self.unit_btns = {}
        for text, u in (("磅 pt", "pt"), ("像素 px", "px")):
            b = ttk.Button(box, text=text, style="Pill.TButton", command=lambda u=u: self.set_unit(u))
            b.pack(side="left", padx=(0, P(8)))
            self.unit_btns[u] = b
        self.setters.append(lambda: [b.configure(style="Chosen.TButton" if u == self.cfg["size_unit"]
                                                 else "Pill.TButton") for u, b in self.unit_btns.items()])
        # 滑块范围：(磅, 像素)
        slider = {"time_size": ((20, 400), (40, 800)), "date_size": ((6, 120), (12, 240))}

        def spec(key):
            def get():
                u = self.cfg["size_unit"]
                lo, hi = slider[key][u == "px"]
                return lo, hi, SIZE_RANGE[u][key], u == "pt", "磅" if u == "pt" else "px"
            return get
        self.number_row(f, "时间字号", "time_size", 0, 0, "", spec=spec("time_size"))
        self.number_row(f, "日期字号", "date_size", 0, 0, "", spec=spec("date_size"),
                        hint="数字可以直接输入，按回车确认；滑块范围以外的数字也行。")

    def set_unit(self, unit):
        """换单位：把字号换算过去，桌面上看起来大小不变。"""
        old = self.cfg["size_unit"]
        if unit == old:
            return
        k = px_per_pt() if unit == "px" else 1 / px_per_pt()
        self.cfg["size_unit"] = unit
        for key in ("time_size", "date_size"):
            self.cfg[key] = clamp_size(self.cfg, key, self.cfg[key] * k)
        self.refresh_widgets()
        self.changed()

    def build_color(self, f):
        P = self.P
        r = self.row(f, "颜色")
        box = ttk.Frame(r)
        box.grid(row=0, column=1, columnspan=3, sticky="w")
        self.swatches = {}
        for hx, name in COLOR_PRESETS:
            s = Swatch(box, P, hx, name, self.set_color)
            s.cv.pack(side="left", padx=(0, P(6)))
            self.swatches[hx] = s
        r = self.row(f, "自定义颜色")
        box = ttk.Frame(r)
        box.grid(row=0, column=1, columnspan=3, sticky="ew")
        self.chip = tk.Canvas(box, width=P(28), height=P(20), bg=CARD, highlightthickness=0, bd=0)
        self.chip.pack(side="left")
        self.color_text = ttk.Label(box, width=9)
        self.color_text.pack(side="left", padx=(P(10), 0))
        ttk.Button(box, text="选择颜色…", style="Pill.TButton", command=self.pick_color).pack(side="right")
        self.number_row(f, "不透明度", "opacity", 10, 100, "%", scale=100)
        self.toggle_row(f, "文字阴影", "shadow", hint="浅色壁纸上更清楚。")
        self.setters.append(self.show_color)

    def build_fonts(self, f):
        P = self.P
        self.font_which = "time_font"
        r = self.row(f, "修改")
        box = ttk.Frame(r)
        box.grid(row=0, column=1, columnspan=3, sticky="w")
        self.font_btns = {}
        for text, key in (("时间数字", "time_font"), ("日期和星期", "text_font")):
            b = ttk.Button(box, text=text, style="Pill.TButton", command=lambda k=key: self.choose_font_slot(k))
            b.pack(side="left", padx=(0, P(8)))
            self.font_btns[key] = b
        r = self.row(f, "当前字体")
        self.font_now = ttk.Label(r, style="Title.TLabel")
        self.font_now.grid(row=0, column=1, columnspan=3, sticky="w")
        r = self.row(f)
        self.font_list = SearchList(r, P, font_items(), self.pick_font,
                                    [(P(230), "w", True), (P(100), "w", True), (P(56), "e", False)],
                                    "搜索字体，比如 雅黑、Segoe")
        self.font_list.frame.grid(row=0, column=0, columnspan=4, sticky="ew")
        r = self.row(f, hint="日期里有中文，要选支持中文的字体（比如微软雅黑、等线、黑体），不然会变成方框。")
        box = ttk.Frame(r)
        box.grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Button(box, text="选择字体文件…", style="Pill.TButton", command=self.pick_font_file).pack(side="left")
        ttk.Button(box, text="用默认字体", style="Pill.TButton", command=lambda: self.pick_font(
            DEFAULT_CONFIG[self.font_which])).pack(side="left", padx=(P(8), 0))
        self.setters.append(self.show_fonts)

    def build_other(self, f):
        P = self.P
        self.toggle_row(f, "开机自动启动", "autostart")
        r = self.row(f, "运行状态")
        self.run_state = ttk.Label(r)
        self.run_state.grid(row=0, column=1, sticky="w")
        self.run_btn = ttk.Button(r, text="启动时钟", style="Pill.TButton", command=self.start_clock)
        self.run_btn.grid(row=0, column=2, columnspan=2, sticky="e")
        r = self.row(f, "设置文件", hint="设置都保存在 wallclock.json 里，一般不用手动改。")
        box = ttk.Frame(r)
        box.grid(row=0, column=1, columnspan=3, sticky="w")
        ttk.Button(box, text="打开设置文件", style="Pill.TButton", command=self.open_file).pack(side="left")
        ttk.Button(box, text="打开所在文件夹", style="Pill.TButton", command=lambda: os.startfile(HERE)).pack(
            side="left", padx=(P(8), 0))
        r = self.row(f, "关于")
        ttk.Label(r, text=f"{APP_NAME} · {APP_NAME_EN}", style="Title.TLabel").grid(
            row=0, column=1, columnspan=3, sticky="w")
        for i, text in enumerate(("免费、高度可配置、极低性能开销的桌面时钟~", CREDITS), 1):
            ttk.Label(r, text=text, style="Sub.TLabel").grid(row=i, column=1, columnspan=3, sticky="w",
                                                             pady=(P(4), 0))

    # ---------------------------------------------------------- 把设置显示到控件上
    def refresh_widgets(self):
        self.loading = True
        try:
            for s in self.setters:
                s()
        finally:
            self.loading = False
        self.show_tz()

    def show_tz(self):
        cols = self.tz_list.cols_of(self.cfg["main_tz"])
        name, detail = (cols[0], " · ".join(c for c in cols[1:] if c)) if cols else (self.cfg["main_tz"], "")
        self.tz_now.configure(text=name)
        now = f"现在 {clock_now(self.cfg):%H:%M:%S}"
        self.tz_sub.configure(text=f"{detail} · {now}" if detail else now)

    def show_color(self):
        c = self.cfg["color"]
        self.chip_photo = ImageTk.PhotoImage(round_box(self.P(28), self.P(20), self.P(4), c, CARD, "#cfcfcf",
                                                       max(1, self.P(1))))
        self.chip.delete("all")
        self.chip.create_image(0, 0, anchor="nw", image=self.chip_photo)
        self.color_text.configure(text=c)
        for hx, s in self.swatches.items():
            s.select(hx.lower() == c.lower())

    def font_name(self, path):
        cols = self.font_list.cols_of(path)
        if not cols:
            return os.path.basename(path)
        text = " · ".join(c for c in cols[:2] if c)
        return text + ("（默认）" if cols[2] else "")

    def choose_font_slot(self, key):
        self.font_which = key
        self.show_fonts()

    def show_fonts(self):
        key = self.font_which
        for k, b in self.font_btns.items():
            b.configure(style="Chosen.TButton" if k == key else "Pill.TButton")
        self.font_list.set(self.cfg[key], (os.path.basename(self.cfg[key]), "自定义", ""))
        self.font_now.configure(text=self.font_name(self.cfg[key]))

    # ---------------------------------------------------------- 改设置
    def changed(self):
        """任何设置变了：重画预览，并把新设置发给桌面上的时钟（都不写硬盘）。"""
        if self.loading:
            return
        if self.draw_job is None:
            self.draw_job = self.root.after(15, self.draw_preview)
        if self.send_job is None:
            self.send_job = self.root.after(60, self.flush_send)

    def flush_send(self):
        self.send_job = None
        send_preview(self.cfg)

    def pick_tz(self, value):
        self.cfg["main_tz"] = value
        self.show_tz()
        self.changed()

    def pick_font(self, path):
        self.cfg[self.font_which] = path
        self.show_fonts()
        self.changed()

    def pick_font_file(self):
        p = filedialog.askopenfilename(parent=self.root, title="选择字体文件",
                                       initialdir=os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
                                       filetypes=[("字体文件", "*.ttf *.ttc *.otf"), ("所有文件", "*.*")])
        if p:
            try:
                ImageFont.truetype(p, 20)
            except Exception:
                messagebox.showwarning(APP_NAME, "这个文件好像不是能用的字体～", parent=self.root)
                return
            self.pick_font(p.replace("\\", "/"))

    def pick_color(self):
        _, hx = colorchooser.askcolor(color=self.cfg["color"], parent=self.root, title="选择时钟颜色")
        if hx:
            self.set_color(hx)

    def set_color(self, hx):
        self.cfg["color"] = hx.lower()
        self.show_color()
        self.changed()

    def set_center(self, x, y, snapping=False):
        x, y = min(1.0, max(0.0, x)), min(1.0, max(0.0, y))
        sx = snapping and abs(x - 0.5) < 0.015
        sy = snapping and abs(y - 0.5) < 0.015
        self.snap = (sx, sy)
        self.cfg["center_x"] = 0.5 if sx else round(x, 4)
        self.cfg["center_y"] = 0.5 if sy else round(y, 4)
        self.refresh_widgets()
        self.changed()

    def reset(self):
        keep = self.cfg["autostart"]
        self.cfg = dict(DEFAULT_CONFIG, autostart=keep)
        self.refresh_widgets()
        self.changed()

    def open_file(self):
        if not os.path.exists(CONFIG_PATH):
            save_config(self.cfg)
        subprocess.Popen(["notepad.exe", CONFIG_PATH])

    def start_clock(self):
        save_config(self.cfg)
        launch()
        self.root.after(1500, self.tick_status)

    def finish(self):
        """完成：这时才写一次 wallclock.json。"""
        self.root.focus_set()                 # 让正在输入的数字框先确认
        self.root.update()
        if self.cfg != self.original:
            save_config(self.cfg)
            sync_autostart(self.cfg["autostart"])
        send_preview(self.cfg)
        self.root.destroy()

    def cancel(self):
        if self.cfg != self.original:
            send_preview(self.original)
        self.root.destroy()

    # ---------------------------------------------------------- 预览
    def draw_preview(self):
        self.draw_job = None
        stamp = wallpaper_stamp()
        if stamp != self.pv_stamp:
            self.pv_base = wallpaper_image(self.pw, self.ph)
            self.pv_stamp = stamp
        now = clock_now(self.cfg)
        key = (time_label(self.cfg, now), style_key(self.cfg))
        if key != self.pv_key:
            # 按真实屏幕大小画，再缩小，和桌面上的效果一模一样
            img, (dx, dy) = render(self.cfg, self.W, self.H, now)
            k = self.pw / self.W
            small = img.resize((max(1, round(img.width * k)), max(1, round(img.height * k))), Image.LANCZOS)
            self.pv_layer = (small, dx * k, dy * k)
            self.pv_key = key
        small, dx, dy = self.pv_layer
        x = int(round(self.cfg["center_x"] * self.pw + dx))
        y = int(round(self.cfg["center_y"] * self.ph + dy))
        frame = self.pv_base.copy()
        frame.paste(small, (x, y), small)
        out = Image.new("RGB", frame.size, hex_rgb(BG))      # 圆角
        out.paste(frame, (0, 0), self.pv_mask)
        self.pv_box = (x, y, x + small.width, y + small.height)
        self.pv_photo = ImageTk.PhotoImage(out)
        self.pv.itemconfigure(self.pv_item, image=self.pv_photo)
        self.pv.itemconfigure(self.pv_gx, state="normal" if self.dragging and self.snap[0] else "hidden")
        self.pv.itemconfigure(self.pv_gy, state="normal" if self.dragging and self.snap[1] else "hidden")

    def pv_press(self, e):
        self.dragging = True
        x0, y0, x1, y1 = self.pv_box
        cx, cy = self.cfg["center_x"] * self.pw, self.cfg["center_y"] * self.ph
        self.grab = (e.x - cx, e.y - cy) if x0 <= e.x <= x1 and y0 <= e.y <= y1 else (0, 0)
        self.pv_move(e)

    def pv_move(self, e):
        self.set_center((e.x - self.grab[0]) / self.pw, (e.y - self.grab[1]) / self.ph, snapping=True)

    def pv_release(self, _):
        self.dragging = False
        self.changed()

    def tick(self):
        self.draw_preview()
        self.show_tz()
        self.tick_status()
        self.root.after(1000 - int(time.time() * 1000) % 1000 + 20, self.tick)

    def tick_status(self):
        running = clock_running()
        self.run_state.configure(text="✓ 正在桌面上运行" if running else "现在没有运行")
        if running:
            self.run_btn.grid_remove()
            self.status.configure(text="可以直接拖动预览里的时钟。改动会马上显示在桌面上，"
                                       "点「完成」保存，点「取消」恢复原样。")
        else:
            self.run_btn.grid()
            self.status.configure(text="时钟现在没有运行：点「完成」保存设置，下次启动时生效。")

    # ---------------------------------------------------------- 全屏预览编辑
    def pick_fullscreen(self):
        """先把设置窗口和桌面上的时钟藏起来，截一张屏幕，再在截图上编辑：方便和桌面上的其它东西对齐。"""
        self.root.withdraw()
        hidden = send_preview(dict(self.cfg, opacity=0.0, shadow=False))
        # 时钟最多 0.2 秒刷新一次，等它擦掉自己、窗口也消失了再截
        self.root.after(450 if hidden else 200, self._fullscreen_shot)

    def _fullscreen_shot(self):
        try:
            from PIL import ImageGrab
            shot = ImageGrab.grab()
            if shot.size != (self.W, self.H):
                shot = shot.resize((self.W, self.H), Image.LANCZOS)
        except Exception as e:
            log(f"截屏失败，改用壁纸：{e!r}")
            shot = wallpaper_image(self.W, self.H)
        self._fullscreen(shot.convert("RGB"))

    def _fullscreen(self, shot):
        W, H, S = self.W, self.H, self.S
        before = {k: self.cfg[k] for k in ("center_x", "center_y", "time_size", "date_size")}
        top = tk.Toplevel(self.root)
        top.title(f"{APP_NAME} · 全屏预览编辑")
        top.overrideredirect(True)
        top.geometry(f"{W}x{H}+0+0")
        top.attributes("-topmost", True)
        cv = tk.Canvas(top, width=W, height=H, highlightthickness=0, bd=0, cursor="fleur", bg="black")
        cv.pack()
        top.bg = ImageTk.PhotoImage(shot)
        cv.create_image(0, 0, anchor="nw", image=top.bg)
        item = cv.create_image(0, 0, anchor="nw")
        dash = dict(fill=ACCENT, dash=(10, 8), width=max(1, int(2 * S)), state="hidden")
        gx = cv.create_line(W // 2, 0, W // 2, H, **dash)
        gy = cv.create_line(0, H // 2, W, H // 2, **dash)
        hint_text = "按住时钟拖到想要的位置  ·  滚轮调大小  ·  回车或双击：确定  ·  Esc 或右键：取消"
        tw = tkfont.Font(family=UI_FONT, size=11).measure(hint_text)
        pw_, ph_, ss = tw + int(48 * S), int(44 * S), 4
        pill = Image.new("RGBA", (pw_ * ss, ph_ * ss), (0, 0, 0, 0))
        ImageDraw.Draw(pill).rounded_rectangle((0, 0, pw_ * ss - 1, ph_ * ss - 1), ph_ * ss // 2,
                                               fill=(28, 28, 28, 191))      # 75% 不透明
        top.pill = ImageTk.PhotoImage(pill.resize((pw_, ph_), Image.LANCZOS))
        cv.create_image(W // 2, int(56 * S), image=top.pill)
        cv.create_text(W // 2, int(56 * S), fill="#ffffff", font=(UI_FONT, 11), text=hint_text)
        st = {"grab": (0, 0), "job": None}

        def relayer():
            st["job"] = None
            img, st["off"] = render(self.cfg, W, H, clock_now(self.cfg))
            st["size"] = img.size
            st["img"] = ImageTk.PhotoImage(img)
            cv.itemconfigure(item, image=st["img"])
            move()

        def move():
            x, y = place(self.cfg, W, H, st["off"])
            cv.coords(item, x, y)

        def press(e):
            x, y = place(self.cfg, W, H, st["off"])
            inside = x <= e.x <= x + st["size"][0] and y <= e.y <= y + st["size"][1]
            cx, cy = W * self.cfg["center_x"], H * self.cfg["center_y"]
            st["grab"] = (e.x - cx, e.y - cy) if inside else (0, 0)
            drag(e)

        def drag(e):
            nx = min(1.0, max(0.0, (e.x - st["grab"][0]) / W))
            ny = min(1.0, max(0.0, (e.y - st["grab"][1]) / H))
            sx, sy = abs(nx - 0.5) < 0.012, abs(ny - 0.5) < 0.012
            self.cfg["center_x"] = 0.5 if sx else round(nx, 4)
            self.cfg["center_y"] = 0.5 if sy else round(ny, 4)
            cv.itemconfigure(gx, state="normal" if sx else "hidden")
            cv.itemconfigure(gy, state="normal" if sy else "hidden")
            move()

        def release(_):
            cv.itemconfigure(gx, state="hidden")
            cv.itemconfigure(gy, state="hidden")

        def wheel(e):
            f = 1.06 if e.delta > 0 else 1 / 1.06
            t = clamp_size(self.cfg, "time_size", self.cfg["time_size"] * f)
            r = t / self.cfg["time_size"]
            self.cfg["time_size"] = t
            self.cfg["date_size"] = clamp_size(self.cfg, "date_size", self.cfg["date_size"] * r)
            if st["job"] is None:
                st["job"] = top.after(30, relayer)

        def done(ok):
            if not ok:
                self.cfg.update(before)
            top.grab_release()
            top.destroy()
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
            self.refresh_widgets()
            self.changed()                    # 顺便让桌面上的时钟重新显示出来

        cv.bind("<ButtonPress-1>", press)
        cv.bind("<B1-Motion>", drag)
        cv.bind("<ButtonRelease-1>", release)
        cv.bind("<Double-Button-1>", lambda e: done(True))
        cv.bind("<Button-3>", lambda e: done(False))
        top.bind("<MouseWheel>", wheel)
        top.bind("<Return>", lambda e: done(True))
        top.bind("<Escape>", lambda e: done(False))
        relayer()
        top.focus_force()
        top.grab_set()

    def run(self):
        self.root.mainloop()


class Tip:
    """鼠标停在色块上时显示颜色名字。"""

    def __init__(self, widget, text):
        self.widget, self.text, self.win = widget, text, None
        widget.bind("<Enter>", self.show, add="+")
        widget.bind("<Leave>", self.hide, add="+")

    def show(self, _):
        x = self.widget.winfo_rootx()
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.win = tk.Toplevel(self.widget)
        self.win.overrideredirect(True)
        self.win.geometry(f"+{x}+{y}")
        tk.Label(self.win, text=self.text, bg=CARD, fg=TEXT, font=(UI_FONT, 9), relief="solid",
                 borderwidth=1, padx=6, pady=2).pack()

    def hide(self, _):
        if self.win:
            self.win.destroy()
            self.win = None


def settings_main():
    global tk, ttk, tkfont, colorchooser, filedialog, messagebox, ImageTk
    kernel32.CreateMutexW(None, False, SETTINGS_MUTEX)
    if ctypes.get_last_error() == 183:             # 设置窗口已经开着了：把它叫到前面
        h = user32.FindWindowW(None, SETTINGS_TITLE)
        if h:
            user32.ShowWindow(h, 9)                 # SW_RESTORE
            user32.SetForegroundWindow(h)
        return
    # 自己的任务栏身份：不然用 .pyw 运行时，任务栏会把它当成 Python，显示 Python 的图标
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ArqiuOwo.ClaudeDesktopClock.Settings")
    except Exception:
        pass
    import tkinter as tk
    from tkinter import colorchooser, filedialog, messagebox, ttk
    from tkinter import font as tkfont
    from PIL import ImageTk
    try:
        SettingsApp().run()
    except Exception as e:
        log(f"设置窗口出错：{e!r}")
        raise


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

    if "--quit" in sys.argv:
        ev = kernel32.OpenEventW(0x0002, False, QUIT_EVENT)  # EVENT_MODIFY_STATE
        if ev:
            kernel32.SetEvent(ev)
        return
    if "--settings" in sys.argv:
        settings_main()
        return

    quit_event = kernel32.CreateEventW(None, True, False, QUIT_EVENT)
    mutex = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    # 已经有一个在运行：让旧的退出，由这次启动的接管（等于「重启」，总是用最新的代码和配置）
    if kernel32.WaitForSingleObject(mutex, 0) != 0:
        kernel32.SetEvent(quit_event)
        if kernel32.WaitForSingleObject(mutex, 10000) not in (0, 0x80):
            log(f"旧的{APP_NAME}没有退出，本次启动取消")
            return
    kernel32.ResetEvent(quit_event)

    clock = WallClock()
    sync_shortcuts()
    try:
        clock.cleanup_old_version()
    except Exception as e:
        log(f"清理旧版本失败：{e!r}")
    handles = (W.HANDLE * 1)(quit_event)
    msg = W.MSG()
    tray = None
    try:
        tray = Tray(clock, lambda: kernel32.SetEvent(quit_event))
    except Exception as e:
        log(f"托盘图标创建失败：{e!r}")
    try:
        while True:
            try:
                clock.update()
            except Exception as e:
                log(f"刷新失败：{e!r}")
            # 最多等 0.2 秒（切换桌面能马上跟上），并且尽量卡在整秒刚过的时候刷新
            ms = min(200, 1000 - int(time.time() * 1000) % 1000 + 15)
            r = user32.MsgWaitForMultipleObjects(1, handles, False, ms, 0x04FF)  # QS_ALLINPUT
            if r == 0:
                break
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
    finally:
        if tray:
            tray.remove()
        clock.close()


if __name__ == "__main__":
    main()
